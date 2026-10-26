# Terminal in your browser

Open **Terminal** in the portal navigation after signing in. Choose **Bastion** to check jobs, or choose one of **My desktop** targets to use your own running Linux desktop. Select **Connect**. Targets include Linux desktops launched after the cluster was upgraded to 26.11.1. To use an older desktop here, launch a new one. Shared desktops, Windows desktops, and desktops that are still starting are not available here.

The shell runs as your cluster account. It opens the tmux session named `web`, so you can disconnect, put your phone to sleep, or open Terminal on another device and return to the same shell on that target. Multiple connections to the same target share that session and its screen size. A host restart, desktop termination, or bastion container replacement ends its tmux session.

## On a phone

* Tap the terminal to open the keyboard. The terminal resizes above it.
* Use **Ctrl**, then **c**, to interrupt a command. Ctrl and Alt stay selected for the next input only. Tap a selected modifier again to cancel it.
* Use the key bar for Esc, Tab, arrows, and punctuation. **A−** and **A+** change the text size without zooming the page.
* Tap **Scroll** (or type **Ctrl+b**, then **[**) to enter tmux copy mode and read earlier output. Use **Up** and **Down** to scroll; a physical keyboard can also use Page Up and Page Down. Press **q** (or **Esc** with the default tmux bindings) to leave copy mode and resume typing commands. Normal wheel scrolling may not show tmux history.
* Select terminal text and use **Copy**. **Paste** uses your clipboard when your browser permits it; the keyboard's paste action also works.
* Links open in a new tab. **Shift Escape** moves keyboard focus back to the controls. The controls announce connection changes; terminal output does not support screen readers.

After a network interruption, Terminal reconnects with increasing delays, up to about 30 seconds. Each attempt obtains fresh credentials and reattaches tmux. After eight consecutive attempts that do not reach a ready shell, reconnecting stops; select **Connect** to try again. Temporary target outages and connection limits are retried, while access denials and SSH trust or configuration failures require attention before reconnecting. Input while disconnected is not queued or replayed. Pasted input is checked in full against a 64 KiB encoded buffer limit before being queued. Oversized pastes are rejected without sending a prefix. Accepted input drains as the connection permits; if it disconnects before the queue drains, the portal warns that some input may have run and discards the unsent remainder. Check the screen before retrying a command whose result you did not see.

**Disconnect** closes this connection without killing tmux. An idle connection closes after 30 minutes without input; each connection can last at most 12 hours. Select Connect again to return. There can be three connected terminals per account across browsers and devices. An administrator can change the timeouts or disable the page.

The terminal uses ghostty-web 0.4.0 and loads its renderer from the portal when you open the page. Wait for **Connect** to become available. If the renderer cannot load, reload the page. Plain HTTP/HTTPS links are clickable; links split across terminal lines and OSC 8 hyperlinks are not supported. Light/dark mode follows the portal, including existing output. Explicit gray text and black backgrounds follow that theme as well.
