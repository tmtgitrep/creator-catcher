# Creator Catcher Server

Creator Catcher is a private, self-hosted web application that checks saved
YouTube creators and downloads new videos with yt-dlp. Only download videos
that you own or are authorized to copy. Completed videos can optionally be
moved to a local or mounted network directory while preserving creator folders.

The web interface shows persistent download totals for each creator and keeps
an expandable list of errors from the most recent scan. Statistics begin when
version 0.3.0 is installed; earlier downloads are not retroactively counted.

## Development

Run tests with: make test

For an unprivileged development server, set CREATOR_CATCHER_STATE_DIR to a
writable directory, CREATOR_CATCHER_YTDLP to /usr/bin/yt-dlp, and run:

    python3 src/creator_catcher/app.py serve

## Debian package

Place the official portable yt-dlp executable at vendor/yt-dlp, then run:

    make package

Install on Raspberry Pi OS:

    sudo apt install ./creator-catcher_0.3.1_all.deb

Verify a downloaded release before installing:

    sha256sum -c creator-catcher_0.3.1_all.deb.sha256

The service initially listens only at 127.0.0.1:8080. Set
CREATOR_CATCHER_HOST in /etc/default/creator-catcher to the Pi's Tailscale
address, then restart creator-catcher-web.service.

Application state and downloads are retained in /var/lib/creator-catcher when
the package is removed.

## SMB storage

Creator Catcher intentionally does not store SMB usernames or passwords in its
web configuration. Mount the share on the Pi with a root-owned credentials file,
then enter the mounted destination in **Move completed videos to**.

For `smb://192.168.50.69/plexmediaserver/Media/FromYouTube`, create the local
mount point and credentials file:

    sudo install -d -m 0755 /mnt/plexmediaserver
    sudo install -m 0600 /dev/null /etc/creator-catcher-smb.credentials
    sudoedit /etc/creator-catcher-smb.credentials

The credentials file should contain these two lines with the SMB account's real
values:

    username=YOUR_SMB_USERNAME
    password=YOUR_SMB_PASSWORD

Add this single line to `/etc/fstab`:

    //192.168.50.69/plexmediaserver /mnt/plexmediaserver cifs credentials=/etc/creator-catcher-smb.credentials,uid=creator-catcher,gid=creator-catcher,file_mode=0664,dir_mode=0775,vers=3.0,_netdev,nofail,x-systemd.automount 0 0

Activate and verify the mount:

    sudo systemctl daemon-reload
    ls /mnt/plexmediaserver
    sudo -u creator-catcher test -w /mnt/plexmediaserver/Media/FromYouTube

Finally save this destination in Creator Catcher:

    /mnt/plexmediaserver/Media/FromYouTube

At the end of every scan, completed video files are copied to a temporary name
on the destination, atomically renamed, and removed from local storage. Files
retain their creator subdirectory. Failed transfers remain in the download
directory and are retried on the next scan. Existing destination files are
never overwritten. Creator Catcher will not create the move-to root itself; it
must already exist, which prevents an unavailable mount from silently receiving
files on the Pi's local disk.

## Plex naming and metadata

New downloads use this structure before they are moved:

    Creator/Creator - YYYY-MM-DD - Video title [YouTube ID].mp4

Pending files downloaded by older releases receive the creator prefix when
they are moved. The YouTube ID is retained to keep filenames unique. Creator
Catcher also asks yt-dlp to embed available title, creator/uploader, upload date,
description, source URL, thumbnail, and chapter metadata in the completed file.

For Plex to use the filename and embedded MP4 metadata, place this content in a
Personal Media library and enable the local media metadata source. Metadata
availability depends on the information exposed by YouTube and on the final
media container.

## Remote access

The web interface has no application-level login in version 0.3.1. Keep it on
a trusted private network. For a Tailscale installation, bind it only to the
Pi's Tailscale address in /etc/default/creator-catcher; do not expose port 8080
through the router. Tailnet ACLs should limit access to trusted devices.

## License

Creator Catcher is released under the MIT License. The bundled yt-dlp program
retains its own Unlicense terms.
