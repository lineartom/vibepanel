#!/bin/sh

FILENAME=vibepanel-$(git describe).tar.gz

tar -czvf "$FILENAME" -s '#^#vibepanel/#'  static templates server.py get-me-fabric.sh requirements.txt install.sh
tar -tf "$FILENAME"
