#!/bin/sh
# undetected_chromedriver pipes Chrome's stdout/stderr and never reads them: once the 64 KiB pipe fills, Chrome's main thread blocks on write and the whole browser (DevTools, chromedriver, agent) hangs.
exec /usr/bin/google-chrome-stable "$@" >/dev/null 2>&1
