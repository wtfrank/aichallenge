<?php

require_once('header.php');
require_once('memcache.php');

?>

<?php if (logged_in_with_valid_credentials()) { ?>
<!--<MarkdownReplacement with="competition-Announcement.md">--><!--</MarkdownReplacement>-->
<?php } else { ?>
<!--<MarkdownReplacement with="competition-Splash.md">--><!--</MarkdownReplacement>-->
<?php } ?>

<!--<MarkdownReplacement with="competition.md">--><!--</MarkdownReplacement>-->

<?php
    $last_game_id = 0;
    if ($memcache)
        $last_game_id = $memcache->get('l:splash');
    if (!$last_game_id) {
        $last_game_id = 0;
    }
    include 'visualizer_widget.php';
    visualize_game($game_id=strval($last_game_id),false,550,550);
?>

<p>Browse other <a href="games.php">recent games here</a>.</p>

<?php

require_once('footer.php');

?>
