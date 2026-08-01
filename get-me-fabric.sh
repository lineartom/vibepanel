#!/bin/bash

[ "$1" != "" ] && cd "$1"

LATEST_MINECRAFT=$(
    wget -O - --quiet https://meta.fabricmc.net/v2/versions/game |
        grep version        |
        grep '\.'           |
        grep -v 'rc'        |
        grep -o '"[0-9.]*"' |
        tr -d '"'           |
        sort -V             |
        tail -n 1
)


LATEST_INSTALLER=$(
    wget -O - --quiet https://meta.fabricmc.net/v2/versions/installer |
        grep version    |
        grep '\.'       |
        grep -v 'rc'    |
        grep -o '"[0-9.]*"' |
        tr -d '"' |
        sort -V |
        tail -n 1
)

LATEST_LOADER=$(
    wget -O - --quiet https://meta.fabricmc.net/v2/versions/loader |
        grep version    |
        grep '\.'       |
        grep -v 'rc'    |
        grep -o '"[0-9.]*"' |
        tr -d '"' |
        sort -V |
        tail -n 1
)

echo "Latest minecraft is ${LATEST_MINECRAFT}"
if [ "$2" != "" ] ; then
	LATEST_MINECRAFT="$2"
	echo "But you requested $LATEST_MINECRAFT"
fi
# https://meta.fabricmc.net/v2/versions/loader/1.20.2/0.14.24/0.11.2/server/jar
URL="https://meta.fabricmc.net/v2/versions/loader/${LATEST_MINECRAFT}/${LATEST_LOADER}/${LATEST_INSTALLER}/server/jar"

echo "DOWNLOAD FROM ${URL}"
# Quoted: $2 lands in ${URL}, and unquoted it would word-split, so a version
# containing whitespace would hand extra arguments to wget (`-O /some/path`
# being the obvious one). VibePanel's own validation forbids spaces, but this
# script is also run by hand.
wget --content-disposition "${URL}"