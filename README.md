# Creator Catcher Server

Creator Catcher is a private, self-hosted web application that checks saved
YouTube creators and downloads new videos with yt-dlp. Only download videos
that you own or are authorized to copy.

## Development

Run tests with: make test

For an unprivileged development server, set CREATOR_CATCHER_STATE_DIR to a
writable directory, CREATOR_CATCHER_YTDLP to /usr/bin/yt-dlp, and run:

    python3 src/creator_catcher/app.py serve

## Debian package

Place the official portable yt-dlp executable at vendor/yt-dlp, then run:

    make package

Install on Raspberry Pi OS:

    sudo apt install ./creator-catcher_0.1.0_all.deb

Verify a downloaded release before installing:

    sha256sum -c creator-catcher_0.1.0_all.deb.sha256

The service initially listens only at 127.0.0.1:8080. Set
CREATOR_CATCHER_HOST in /etc/default/creator-catcher to the Pi's Tailscale
address, then restart creator-catcher-web.service.

Application state and downloads are retained in /var/lib/creator-catcher when
the package is removed.

## Remote access

The web interface has no application-level login in version 0.1.0. Keep it on
a trusted private network. For a Tailscale installation, bind it only to the
Pi's Tailscale address in /etc/default/creator-catcher; do not expose port 8080
through the router. Tailnet ACLs should limit access to trusted devices.

## License

Creator Catcher is released under the MIT License. The bundled yt-dlp program
retains its own Unlicense terms.
