#!/bin/bash
export PYTHONPATH=/data/project/shared/pywikibot/core:/shared/pywikibot/core/scripts
export PIPENV_VENV_IN_PROJECT=1
exec /data/project/dewikigreetbot/.pyenv/shims/pipenv run python /data/project/dewikigreetbot/stats.py -v -log:stats.log
