# Format a post with Micron

Choose **Micron** in the web composer's **Format** selector. Write your post,
sign in, and use **Preview** to check it before publishing. The composer adds
`#!micron` on the first line; leave that marker in place. Choosing **Plain text**
removes the marker and keeps the rest of your source.

From MeshCore, Meshtastic, or LXMF, start a post through the normal `help` →
**3 Write** menu. After sending its title, send `#!micron` as the first message
of the body. Send your formatted text in following messages, then `done` to
review and `publish` to post. The same marker works in an advanced multipart
draft or a body submitted through the web API.

For example:

```text
#!micron
>Saturday field day
`!Meet at 9 AM`! at the trailhead.
Bring a `*charged radio`* and `_water`_.

`Ff80Weather permitting`f
`[Details`https://coloradomesh.org]
-
See you there!
```

The post is formatted in the web reader, RSS, and its full NomadNet page.
MeshCore, Meshtastic, packet terminals, and LXMF show readable text with the
formatting controls removed. Links retain their destinations. The radio draft
preview uses that text version too. These are different views of the same post;
sync preserves the original body and its signature.

## Supported formatting

| Source | Result |
| --- | --- |
| `>Heading`, `>>Heading`, `>>>Heading` | Three heading levels |
| `` `!bold`! `` | Bold |
| `` `*italic`* `` | Italic |
| `` `_underline`_ `` | Underline |
| `` `Ff80text`f `` | Foreground color; `f` restores the default |
| `` `B222text`b `` | Background color; `b` restores the default |
| `` `FTff8800text`f `` | Six-digit foreground color; `BT` also works |
| `` `c ``, `` `l ``, `` `r ``, `` `a `` | Center, left, right, or default alignment |
| Two backticks | Reset emphasis, colors, and alignment |
| A line starting with `-` | Divider |
| A line starting with `#` | Comment, omitted from the displayed post |
| A backslash before a backtick | Display the backtick literally |
| `\>`, `\#`, `\-` at the start of a line | Display the initial character literally |

Emphasis, colors, and alignment continue across lines until reset. Surround a
literal block with a line containing a backtick followed by `=` on each side.
Inside it, formatting is displayed as text. To show that delimiter itself inside
the block, precede it with a backslash.

Links use a backtick, `[`, a label, another backtick, the address, and `]`.
HTTP and HTTPS links are clickable in the web reader. Full NomadNet addresses
such as `0123456789abcdef0123456789abcdef:/page/index.mu` are clickable in
NomadNet and displayed as addresses on the web. Links with form fields, relative
addresses, credentials, or other schemes stay literal.

## Post formatting, not executable pages

This is a display-only Micron subset. Forms, submission fields, embedded or
refreshing pages, page directives, and table directives are shown as text.
There are no automatic resource loads from a post. Unsupported controls remain
visible; raw HTML remains text. Dividers use the reader's standard line.

Formatting ends at the post boundary so it cannot change BBS navigation or the
next article. Extremely fragmented formatting falls back to literal source;
large NomadNet output falls back to readable text to stay within the page limit.
Titles and author names always stay plain text. Posts without the first-line
marker keep their existing plain-text behavior. Older BBS versions can replicate
Micron posts but display the source until upgraded.

The syntax follows the display controls in the
[NomadNet Micron parser](https://github.com/markqvist/NomadNet/blob/master/nomadnet/ui/textui/MicronParser.py).
