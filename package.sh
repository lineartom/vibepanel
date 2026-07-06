#!/bin/sh

VERSION=$(git describe)
FILENAME=vibepanel-$VERSION.tar.gz

echo "$VERSION" > VERSION
tar -czvf "$FILENAME" -s '#^#vibepanel/#'  static templates server.py get-me-fabric.sh requirements.txt install.sh VERSION
rm VERSION
tar -tf "$FILENAME"
