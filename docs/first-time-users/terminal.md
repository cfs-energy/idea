# Terminal in your browser

Select **Shell** in the portal's top bar for a terminal on the bastion, for example to check jobs. For your own running Linux desktop, open **My desktops** and select **Terminal** on the desktop, or choose **Terminal** from its **Actions** menu. **Shell** appears only when the cluster has a bastion you can use.

**Terminal** is unavailable, with the reason shown, on Windows desktops, on desktops that are stopped or still starting, and on desktops launched before the cluster ran 26.11.1. To use an older desktop here, launch a new one. Shared desktops have no Terminal.

Terminals open in a panel at the bottom of the portal, one tab for the bastion and one for each desktop. The panel stays open while you move between pages. Drag its top edge to resize it; the browser remembers the height. Use the arrow at the right to minimize or restore it, and **×** on a tab to close that terminal.

The shell runs as your cluster account. It opens the tmux session named `web`, so you can close a tab, put your phone to sleep, or open a terminal on another device and return to the same shell on that target. Multiple connections to the same target share that session and its screen size. A host restart, desktop termination, or bastion container replacement ends its tmux session.

## Copy and paste

* Select text with the mouse. Copy with **Cmd+C** on macOS or **Ctrl+Shift+C** on Windows and Linux.
* Paste with **Cmd+V** on macOS or **Ctrl+Shift+V** on Windows and Linux, or right-click and choose **Paste**.
* **Ctrl+C** always interrupts the running command.

## On a phone

* Tap the terminal to open your keyboard. The panel fills the screen and stays above the keyboard. Use the arrow at the top right to return to the portal.
* Drag to scroll and pinch to change the text size.
* Use the space bar or the cursor to move along the command line, as you would when editing text.
* Long-press to select text. Use the system menu to copy, or to paste into the shell. Select **Done** to return.
* The key bar adds **Ctrl**, **Esc**, **Tab** and arrows. Use **Ctrl**, then **c**, to interrupt a command; Ctrl applies to the next key only. The key bar hides once you type on a hardware keyboard.
* Tap **Scroll** (or type **Ctrl+b**, then **[**) to enter tmux copy mode and read earlier output. Drag or use **Up** and **Down** to scroll. Press **q** to leave copy mode.

Links open in a new tab. **Shift Escape** moves keyboard focus from the terminal to its controls. The controls announce connection changes; terminal output does not support screen readers.

## Connections

A terminal connects when its tab opens. After a network interruption, it reconnects with increasing delays, up to about 30 seconds. Each attempt obtains fresh credentials and reattaches tmux. After eight consecutive attempts that do not reach a ready shell, reconnecting stops; select **Connect** to try again. Temporary target outages and connection limits are retried, while access denials and SSH trust or configuration failures require attention before reconnecting. Input while disconnected is not queued or replayed. Pasted input is checked in full against a 64 KiB encoded buffer limit before being queued. Oversized pastes are rejected without sending a prefix. Accepted input drains as the connection permits; if it disconnects before the queue drains, the portal warns that some input may have run and discards the unsent remainder. Check the screen before retrying a command whose result you did not see.

**Disconnect** closes the connection without killing tmux. An idle connection closes after 30 minutes without input; each connection can last at most 12 hours. Select Connect again to return. There can be three connected terminals per account across browsers and devices; close a tab you no longer need before opening a fourth. An administrator can change the timeouts or turn terminals off.

The terminal uses ghostty-web 0.4.0 and loads its renderer from the portal when you open the first tab. If the renderer cannot load, reload the page. Plain HTTP/HTTPS links are clickable; links split across terminal lines and OSC 8 hyperlinks are not supported. Light/dark mode follows the portal, including existing output. Explicit gray text and black backgrounds follow that theme as well.
