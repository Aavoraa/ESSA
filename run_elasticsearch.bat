@echo off
REM Start the local Elasticsearch node ESSA reads its search/analytics index from.
REM
REM ESSA does not need it to run: PostgreSQL is the source of truth, and every
REM read that uses Elasticsearch falls back to PostgreSQL when it is down or
REM behind (see backend/app/search). Start this before run.bat to get the fast
REM path; stop it with Ctrl-C in its window.
REM
REM Installed at C:\elasticsearch (no spaces in the path - the node's own
REM scripts do not cope with them). Loopback only, single node, 2 GB heap.
set "ES_HOME=C:\elasticsearch\elasticsearch-9.5.4"
if not exist "%ES_HOME%\bin\elasticsearch.bat" (
  echo Elasticsearch is not installed at %ES_HOME%
  exit /b 1
)
title Elasticsearch (ESSA)
"%ES_HOME%\bin\elasticsearch.bat"
