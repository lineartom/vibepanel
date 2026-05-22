#!/bin/sh

tar -czvf vibepanel.tar.gz -s '#^#vibepanel/#'  static templates server.py get-me-fabric.sh requirements.txt install.sh
tar -tf vibepanel.tar.gz
