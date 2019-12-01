#!/bin/bash
echo $(date) Script started...
export PYTHONPATH=/data/project/shared/pywikibot/core:/shared/pywikibot/core/scripts
export GREETBOT_SECRET=`cat /data/project/dewikigreetbot/secret`
export PIPENV_VENV_IN_PROJECT=1
exec /data/project/dewikigreetbot/.pyenv/shims/pipenv run python /data/project/dewikigreetbot/greetbot.py -v -log:greetbot.log --run-bot
