#!/bin/sh
# Wrapper for an app's autostart Exec= line: wait for every /etc/fstab mount
# marked 'nofail' to actually be mounted (up to $TUXTHROTTLE_MOUNTWAIT_TIMEOUT
# seconds, default 10), then exec the real command. A 'nofail' external drive
# (NTFS/exFAT Steam library, etc.) can still be mid-mount when the desktop
# session's autostart apps fire, so an app that reads it immediately (Steam
# scanning its library folders) sees it as missing for a few seconds. No-op
# if there are no 'nofail' entries or they're already mounted.
timeout="${TUXTHROTTLE_MOUNTWAIT_TIMEOUT:-10}"
end=$(($(date +%s) + timeout))

mounts=$(awk '$4 ~ /(^|,)nofail(,|$)/ { print $2 }' /etc/fstab 2>/dev/null)

if [ -n "$mounts" ]; then
    while [ "$(date +%s)" -lt "$end" ]; do
        pending=0
        for m in $mounts; do
            m=$(printf '%s' "$m" | sed 's/\\040/ /g')
            mountpoint -q "$m" 2>/dev/null || pending=1
        done
        [ "$pending" -eq 0 ] && break
        sleep 0.5
    done
fi

exec "$@"
