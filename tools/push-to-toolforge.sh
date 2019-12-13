#!/bin/bash

pscp ../{greetbot.py,stats.py,Pipfile,Pipfile.lock} exec-bot.sh update-stats.sh *.yaml countcount@login.tools.wmflabs.org:/data/project/dewikigreetbot/
plink countcount@login.tools.wmflabs.org "chmod 755 /data/project/dewikigreetbot/exec-bot.sh"
plink countcount@login.tools.wmflabs.org "chmod 755 /data/project/dewikigreetbot/update-stats.sh"
plink countcount@login.tools.wmflabs.org become dewikigreetbot kubectl delete pods --all
