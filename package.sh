#!/bin/sh

set -eu

VERSION=${VERSION:-$(git describe)}
FILENAME=vibepanel-$VERSION.tar.gz

FILES="static templates server.py get-me-fabric.sh requirements.txt install.sh vibepanel.service VERSION"

echo "$VERSION" > VERSION

# Both tars can prefix paths with vibepanel/, but spell it differently:
# GNU (Linux, the CI runners) wants --transform, BSD (macOS) wants -s.
if tar --version 2>/dev/null | grep -q GNU; then
    tar -czf "$FILENAME" --transform 's#^#vibepanel/#' $FILES
else
    tar -czf "$FILENAME" -s '#^#vibepanel/#' $FILES
fi

rm VERSION
tar -tf "$FILENAME"
