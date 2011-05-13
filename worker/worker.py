#!/usr/bin/env python
from __future__ import print_function
import sys
import os
import json
import urllib
import tempfile
import logging
import logging.handlers
import shutil
import hashlib
import time
import stat
import platform
import traceback
import tempfile

from optparse import OptionParser

from server_info import server_info
from submission_hash import hash_file_sha

import compiler
from engine import run_game

# Set up logging
log = logging.getLogger('worker')
log.setLevel(logging.INFO)
log_file = os.path.join(server_info['logs_path'], 'worker.log')
handler = logging.handlers.RotatingFileHandler(log_file,
                                               maxBytes=1000000,
                                               backupCount=5)
handler.setLevel(logging.INFO)
handler2 = logging.StreamHandler()
formatter = logging.Formatter("%(asctime)s - " + str(os.getpid()) +
                              " - %(levelname)s - %(message)s")
handler.setFormatter(formatter)
handler2.setFormatter(formatter)
log.addHandler(handler)
log.addHandler(handler2)

handler2 = logging.StreamHandler()


STATUS_CREATED = 10
STATUS_UPLOADED = 20
STATUS_COMPILING = 30
# these 4 will be returned by the worker
STATUS_RUNABLE = 40
STATUS_DOWNLOAD_ERROR = 50
STATUS_UNPACK_ERROR = 60
STATUS_COMPILE_ERROR = 70
STATUS_TEST_ERROR = 80

# get game from ants dir
sys.path.append("../ants")
from ants import Ants

class CD(object):
    def __init__(self, new_dir):
        self.new_dir = new_dir

    def __enter__(self):
        self.org_dir = os.getcwd()
        os.chdir(self.new_dir)
        return self.new_dir

    def __exit__(self, type, value, traceback):
        os.chdir(self.org_dir)
          
class GameAPIClient:
    def __init__(self, base_url, api_key):
        self.base_url = base_url
        self.api_key = api_key
    
    def get_url(self, method):
        return '%s/%s.php?api_key=%s' % (self.base_url, method, self.api_key)

    def get_task(self):
        try:
            url = self.get_url('api_get_task')
            log.debug(url)
            data = urllib.urlopen(url).read()
            return json.loads(data)
        except ValueError as ex:
            log.error("Bad json from server during get task: %s" % data)
            return None
        except Exception as ex:
            log.error("Get task error: %s" % ex)
            return None
    
    def get_submission_hash(self, submission_id):
        try:
            url = self.get_url('api_get_submission_hash')
            url += '&submission_id=%s' % submission_id
            data = json.loads(urllib.urlopen(url).read())
            return data['hash']
        except ValueError as ex:
            log.error("Bad json from server during get sumbission hash: %s" % data)
            return None
        except Exception as ex:
            log.error("Get submission hash error: %s" % ex)
            return None
    
    def get_submission(self, submission_id, download_dir):
        try:
            url = self.get_url('api_get_submission')
            url += '&submission_id=%s' % submission_id
            log.debug(url)
            remote_zip = urllib.urlopen(url)
            filename = remote_zip.info().getheader('Content-disposition').split('filename=')[1]
            filename = os.path.join(download_dir, filename)
            local_zip = open(filename, 'wb')
            local_zip.write(remote_zip.read())
            local_zip.close()
            remote_zip.close()
            return filename
        except Exception as ex:
            log.error(traceback.format_exc())
            log.error("Get submission error: %s" % ex)
            return None

    def get_map(self, map_filename):
        try:
            url = '%s/maps/%s' % (self.base_url, map_filename)
            log.info("Downloading map %s" % url)
            data = urllib.urlopen(url).read()
            return data.read()
        except Exception as ex:
            log.error("Get map error: %s" % ex)
            return None

    # TODO: save failed posts locally and retry on worker startup
    def post_result(self, method, result):
        # retry 10 times or until post is successful
        retry = 1
        for i in range(retry):
            url = self.get_url(method)
            log.info(url)
            json_data = json.dumps(result)
            hash = hashlib.md5(json_data).hexdigest()
            log.debug("Posting result %s: %s" % (method, json_data))
            log.info("Posting hash: %s" % hash)
            response = urllib.urlopen(url, json.dumps(result))
            if response.getcode() == 200:
                data = response.read()
                try:
                    log.debug(data)
                    data = json.loads(data)["hash"]
                    log.info("Server returned hash: %s" % data)
                    if hash == data:
                        break
                    elif i < retry-1:
                        time.sleep(5)
                except ValueError as ex:
                    log.info("Bad json from server during post result: %s" % data)
                    if i < retry-1:
                        time.sleep(5)                    
            else:
                log.warning("Server did not receive post: %s, %s" % (response.getcode(), response.read()))
                time.sleep(5)

class Worker:
    def __init__(self):
        self.cloud = GameAPIClient( server_info['api_base_url'], server_info['api_key'])
        self.post_id = 0
        self.test_map = None
        self.download_dirs = {}

    def submission_dir(self, submission_id):
        return os.path.join(server_info["compiled_path"], str(submission_id//1000), str(submission_id))
        
    def download_dir(self, submission_id):
        if submission_id not in self.download_dirs:
            tmp_dir = tempfile.mkdtemp(dir=server_info["compiled_path"])
            self.download_dirs[submission_id] = tmp_dir
        return self.download_dirs[submission_id]
    
    def clean_download(self, submission_id):
        if submission_id in self.download_dirs:
            if os.path.exists(self.download_dirs[submission_id]):
                os.rmdir(self.download_dirs[submission_id])
            del self.download_dirs[submission_id]

    def download_submission(self, submission_id):
        submission_dir = self.submission_dir(submission_id)
        download_dir = self.download_dir(submission_id)
        if os.path.exists(submission_dir):
            log.info("Already downloaded and compiled: %s..." % submission_id)
            return True
        elif len(os.listdir(download_dir)) > 0:
            log.info("Already downloaded: %s..." % submission_id)
            return True
        else:
            log.info("Downloading %s..." % submission_id)
            os.chmod(download_dir, 0755)
            filename = self.cloud.get_submission(submission_id, download_dir)
            if filename != None:
                remote_hash = self.cloud.get_submission_hash(submission_id)
                with open(filename, 'rb') as f:
                    local_hash = hashlib.md5(f.read()).hexdigest()
                if local_hash != remote_hash:
                    log.error("After downloading submission %s to %s hash didn't match" %
                            (submission_id, download_dir))
                    log.error("local_hash: %s , remote_hash: %s" % (local_hash, remote_hash))
                    shutil.rmtree(download_dir)
                    log.error("Hash error.")
                    return False
                return True
            else:
                shutil.rmtree(download_dir)
                log.error("Submission not found on server.")
                return False
        
    def unpack(self, submission_id):
        download_dir = self.download_dir(submission_id)
        log.info("Unpacking %s..." % download_dir)
        with CD(download_dir):
            if platform.system() == 'Windows':
                zip_files = [
                    ("entry.tar.gz", "7z x -obot -y entry.tar.gz > NUL"),
                    ("entry.tgz", "7z x -obot -y entry.tgz > NUL"),
                    ("entry.zip", "7z x -obot -y entry.zip > NUL")
                ]
            else:
                zip_files = [
                    ("entry.tar.gz", "mkdir bot; tar xfz -C bot entry.tar.gz > /dev/null 2> /dev/null"),
                    ("entry.tgz", "mkdir bot; tar xfz -C bot entry.tgz > /dev/null 2> /dev/null"),
                    ("entry.zip", "unzip -u -dbot entry.zip > /dev/null 2> /dev/null")
                ]
            for file_name, command in zip_files:
                if os.path.exists(file_name):
                    log.info("unnzip status: %s" % os.system(command))
                    for dirpath, dirnames, filenames in os.walk(".."):
                        os.chmod(dirpath, 0755)
                        for filename in filenames:
                            filename = os.path.join(dirpath, filename)
                            os.chmod(filename,stat.S_IMODE(os.stat(filename).st_mode) | stat.S_IRGRP | stat.S_IROTH)
                    break
            else:
                return False
            return True

    def compile(self, submission_id=None, report_status=False, run_test=True):
        def report(status):
            if report_status:
                self.post_id += 1
                result = {"post_id": self.post_id,
                          "submission_id": submission_id, 
                          "status_id": status }
                self.cloud.post_result('api_compile_result', result)
        if submission_id == None:
            # compile in current directory
            compiler.compile_anything()
        else:
            submission_dir = self.submission_dir(submission_id)
            download_dir = self.download_dir(submission_id)
            if os.path.exists(submission_dir):
                log.info("Already compiled: %s" % submission_id)
                if not run_test or self.functional_test(submission_id):
                    report(STATUS_RUNABLE)
                    return True
                else:
                    report(STATUS_TEST_ERROR)
                    return False
            if len(os.listdir(download_dir)) == 0:
                if not self.download_submission(submission_id):
                    report(STATUS_DOWNLOAD_ERROR)
                    log.error("Download Error")
                    return False
            if len(os.listdir(download_dir)) == 1:
                if not self.unpack(submission_id):
                    report(STATUS_UNPACK_ERROR)
                    log.error("Unpack Error")
                    return False
            log.info("Compiling %s " % submission_id)
            bot_dir = os.path.join(download_dir, 'bot')
            detected_lang, errors = compiler.compile_anything(bot_dir)
            if not detected_lang:
                shutil.rmtree(download_dir)
                log.error('\n'.join(errors))
                report(STATUS_COMPILE_ERROR);
                log.error("Compile Error")
                return False
            else:
                if not os.path.exists(os.path.split(submission_dir)[0]):
                    os.makedirs(os.path.split(submission_dir)[0])
                if not run_test or self.functional_test(submission_id):
                    os.rename(download_dir, submission_dir)
                    del self.download_dirs[submission_id]
                    report(STATUS_RUNABLE)
                    return True
                else:
                    log.info("Functional Test Failure")
                    report(STATUS_TEST_ERROR)
                    return False

    def check_hash(self, submission_id):
        try:
            for filename in os.listdir(os.path.join(server_info["compiled_path"], str(submission_id))):
                if filename.endswith(".zip") or filename.endswith(".tgz"):
                    log.info("%s: %s" % (filename, hash_file_sha1(filename)))
        except:
            log.error("Submission path not found.")
    
    def get_map(self, map_filename):
        map_file = os.path.join(server_info["maps_path"], map_filename)
        if not os.path.exists(map_file):
            data = self.cloud.get_map(map_filename)
            if data == None:
                raise Exception("map", "Could not download map from main server.")
            f = open(map_file, 'w')
            f.write(data)
            f.close()
        else:
            f = open(map_file, 'r')
            data = f.read()
            f.close()
        return data
    
    def get_test_map(self):
        if self.test_map == None:
            f = open('../ants/submission_test/test.map', 'r')
            self.test_map = f.read()
            f.close()
        return self.test_map
    
    def functional_test(self, submission_id):
        self.post_id += 1
        log.info("Running functional test for %s" % submission_id)
        options = server_info["game_options"]
        options['strict'] = True # kills bot on invalid inputs
        options['food'] = 'none'
        options['turns'] = 30
        log.debug(options)
        options["map"] = self.get_test_map()
        options['capture_errors'] = True
        game = Ants(options)
        # options['verbose_log'] = sys.stdout
        # options['error_logs'] = [sys.stdout, None]
        if submission_id in self.download_dirs:
            bot_dir = self.download_dirs[submission_id]
        else:
            bot_dir = self.submission_dir(submission_id)
        bots = [(os.path.join(bot_dir, 'bot'),
                 compiler.get_run_cmd(bot_dir)),
                ("../ants/submission_test/", "python TestBot.py")]
        log.debug(bots)
        result = run_game(game, bots, options)
        log.info(result['status'][0])
        for error in result['errors'][0]:
            log.info(error)
            
        if result['status'][1] in ('crashed', 'timeout', 'invalid'):
            raise Exception('TestBot is not operational')
        if result['status'][0] in ('crashed', 'timeout', 'invalid'):
            return False
        return True
        
    def game(self, task, report_status=False):
        self.post_id += 1
        try:
            matchup_id = int(task["matchup_id"])
            log.info("Running game %s..." % matchup_id)
            if 'options' in task:
                options = task["options"]
            else:
                options = server_info["game_options"]
            options["map"] = self.get_map(task['map_filename'])
            options["output_json"] = True
            game = Ants(options)
            bots = []
            for submission_id in task["submissions"]:
                submission_id = int(submission_id)
                if self.compile(submission_id, run_test=False):
                    submission_dir = self.submission_dir(submission_id)
                    run_cmd = compiler.get_run_cmd(submission_dir)
                    #run_dir = tempfile.mkdtemp(dir=server_info["compiled_path"])
                    bot_dir = os.path.join(submission_dir, 'bot')
                    bots.append((bot_dir, run_cmd))
                    #shutil.copytree(submission_dir, run_dir)
                else:
                    self.clean_download(submission_id)
                    raise Exception('bot', 'Can not compile bot %s' % submission_id)
            options['game_id'] = matchup_id
            log.debug((game.__class__.__name__, task['submissions'], options, matchup_id))
            result = run_game(game, bots, options)
            log.debug(result)
            del result['game_id']
            result['matchup_id'] = matchup_id
            result['post_id'] = self.post_id
            if report_status:
                self.cloud.post_result('api_game_result', result)
        except Exception as ex:
            import traceback
            log.debug(traceback.format_exc())
            result = {"post_id": self.post_id,
                      "matchup_id": matchup_id,
                      "error": str(ex) }
            self.cloud.post_result('api_game_result', result)
            
    def task(self, last=False):
        task = self.cloud.get_task()
        if task:
            try:
                log.info("Recieved task: %s" % task)
                if task['task'] == 'compile':
                    submission_id = int(task['submission_id'])
                    if not self.compile(submission_id, True):
                        self.clean_download(submission_id)
                elif task['task'] == 'game':
                    self.game(task, True)
                else:
                    if not last:
                        time.sleep(20)
            except:
                log.error('Task Failure')
                log.error(traceback.format_exc())
                quit()
        else:
            log.error("Error retrieving task from server.")

def main(argv):
    usage ="""Usage: %prog [options]\nThe worker will not attempt to retrieve
    tasks from the server if a specifec submission_id is given."""
    parser = OptionParser(usage=usage)
    parser.add_option("-s", "--submission_id", dest="submission_id",
                      type="int", default=0,
                      help="Submission id to use for hash, download and compile")
    parser.add_option("--hash", dest="hash",
                      action="store_true", default=False,
                      help="Display submission hash")
    parser.add_option("-d", "--download", dest="download",
                      action="store_true", default=False,
                      help="Download submission")
    parser.add_option("-c", "--compile", dest="compile",
                      action="store_true", default=False,
                      help="Compile current directory or submission")
    parser.add_option("-t", "--task", dest="task",
                      action="store_true", default=False,
                      help="Get next task from server")
    parser.add_option("-n", "--num_tasks", dest="num_tasks",
                      type="int", default=1,
                      help="Number of tasks to get from server")
    
    (opts, args) = parser.parse_args(argv)
    
    worker = Worker()

    # if the worker is not run in task mode, it will not clean up the download
    #    dir, so that debugging can be done on what had been downloaded/unzipped

    # print hash values for submission, must be downloaded
    if opts.submission_id != 0 and opts.hash:
        worker.check_hash(opts.submission_id)
        return

    # download and compile
    if opts.submission_id != 0 and opts.download and opts.compile:
        worker.compile(opts.submission_id)
        return
    
    # download submission
    if opts.submission_id != 0 and opts.download:
        if worker.download_submission(opts.submission_id):
            worker.unpack(opts.submission_id)
        return

    # compile submission
    if opts.submission_id != 0 and opts.compile:
        worker.compile(opts.submission_id)
        return
    
    # compile in current directory
    if opts.compile:
        worker.compile()
        return   
    
    # get tasks
    if opts.task:
        if opts.num_tasks <= 0:
            try:
                while True:
                    log.info("Getting task infinity + 1")
                    worker.task()
            except KeyboardInterrupt:
                log.info("[Ctrl] + C, Stopping worker")
        else:
            for task_count in range(opts.num_tasks):
                log.info("Getting task %s" % (task_count + 1))
                worker.task((task_count+1)==opts.num_tasks)
        return
    
    parser.print_help()
    
if __name__ == '__main__':
    main(sys.argv[1:])
