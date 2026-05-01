#!/bin/sh

tar -czvf vibepanel.tar.gz -s '#^#vibepanel/#'  static templates server.py requirements.txt install.sh
tar -tf vibepanel.tar.gz
