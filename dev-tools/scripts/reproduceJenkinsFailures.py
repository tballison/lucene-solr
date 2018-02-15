# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import os
import re
import subprocess
import sys
import traceback
import urllib.error
import urllib.request
from textwrap import dedent

# Example: Checking out Revision e441a99009a557f82ea17ee9f9c3e9b89c75cee6 (refs/remotes/origin/master)
reGitRev = re.compile(r'Checking out Revision (\S+)\s+\(refs/remotes/origin/([^)]+)')

# Method example: NOTE: reproduce with: ant test  -Dtestcase=ZkSolrClientTest -Dtests.method=testMultipleWatchesAsync -Dtests.seed=6EF5AB70F0032849 -Dtests.slow=true -Dtests.locale=he-IL -Dtests.timezone=NST -Dtests.asserts=true -Dtests.file.encoding=UTF-8
# Suite example:  NOTE: reproduce with: ant test  -Dtestcase=CloudSolrClientTest -Dtests.seed=DB2DF2D8228BAF27 -Dtests.multiplier=3 -Dtests.slow=true -Dtests.locale=es-AR -Dtests.timezone=America/Argentina/Cordoba -Dtests.asserts=true -Dtests.file.encoding=US-ASCII
reReproLine = re.compile(r'NOTE:\s+reproduce\s+with:(\s+ant\s+test\s+-Dtestcase=(\S+)\s+(?:-Dtests.method=\S+\s+)?(.*))')
reTestsSeed = re.compile(r'-Dtests.seed=\S+\s*')

# Example: https://jenkins.thetaphi.de/job/Lucene-Solr-master-Linux/21108/
reJenkinsURLWithoutConsoleText = re.compile(r'https?://.*/\d+/?\Z', re.IGNORECASE)

reJavaFile = re.compile(r'(.*)\.java\Z')
reModule = re.compile(r'\./(.*)/src/')
reTestOutputFile = re.compile(r'TEST-(.*\.([^-.]+))(?:-\d+)?\.xml\Z')
reErrorFailure = re.compile(r'(?:errors|failures)="[^0]')

# consoleText from Policeman Jenkins's Windows jobs fails to decode as UTF-8
encoding = 'iso-8859-1'

lastFailureCode = 0
gitCheckoutSucceeded = False

description = dedent('''\
                     Must be run from a Lucene/Solr git workspace. Downloads the Jenkins
                     log pointed to by the given URL, parses it for Git revision and failed
                     Lucene/Solr tests, checks out the Git revision in the local workspace,
                     groups the failed tests by module, then runs
                     'ant test -Dtest.dups=%d -Dtests.class="*.test1[|*.test2[...]]" ...'
                     in each module of interest, failing at the end if any of the runs fails.
                     To control the maximum number of concurrent JVMs used for each module's
                     test run, set 'tests.jvms', e.g. in ~/lucene.build.properties
                     ''')
defaultIters = 5

def readConfig():
  parser = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter,
                                   description=description)
  parser.add_argument('url', metavar='URL',
                      help='Points to the Jenkins log to parse')
  parser.add_argument('--no-fetch', dest='fetch', action='store_false', default=True,
                      help='Do not run "git fetch" prior to "git checkout"')
  parser.add_argument('--iters', dest='testIters', type=int, default=defaultIters, metavar='N',
                      help='Number of iterations per test suite (default: %d)' % defaultIters)
  return parser.parse_args()

def runOutput(cmd):
  print('[repro] %s' % cmd)
  try:
    return subprocess.check_output(cmd.split(' '), universal_newlines=True).strip()
  except CalledProcessError as e:
    raise RuntimeError("ERROR: Cmd '%s' failed with exit code %d and the following output:\n%s" 
                       % (cmd, e.returncode, e.output))

# Remembers non-zero exit code in lastFailureCode unless rememberFailure==False
def run(cmd, rememberFailure=True):
  global lastFailureCode
  print('[repro] %s' % cmd)
  code = os.system(cmd)
  if 0 != code and rememberFailure:
    print('\n[repro] Setting last failure code to %d\n' % code)
    lastFailureCode = code
  return code

def fetchAndParseJenkinsLog(url):
  global revisionFromLog
  global branchFromLog
  revisionFromLog = None
  tests = {}
  print('[repro] Jenkins log URL: %s\n' % url)
  try:
    with urllib.request.urlopen(url) as consoleText:
      for rawLine in consoleText:
        line = rawLine.decode(encoding)
        match = reGitRev.match(line)
        if match is not None:
          revisionFromLog = match.group(1)
          branchFromLog = match.group(2)
          print('[repro] Revision: %s\n' % revisionFromLog)
        else:
          match = reReproLine.search(line)
          if match is not None:
            print('[repro] Repro line: %s\n' % match.group(1))
            testcase = match.group(2)
            reproLineWithoutMethod = match.group(3).strip()
            tests[testcase] = reproLineWithoutMethod
  except urllib.error.URLError as e:
    raise RuntimeError('ERROR: fetching %s : %s' % (url, e))
  
  if revisionFromLog == None:
    if reJenkinsURLWithoutConsoleText.match(url):
      print('[repro] Not a Jenkins log. Appending "/consoleText" and retrying ...\n')
      fetchAndParseJenkinsLog(url + '/consoleText')                                                        
    else:
      raise RuntimeError('ERROR: %s does not appear to be a Jenkins log.' % url)
  if 0 == len(tests):
    print('[repro] No "reproduce with" lines found; exiting.')
    sys.exit(0)
  return tests

def prepareWorkspace(fetch, gitRef):
  global gitCheckoutSucceeded
  if fetch:
    code = run('git fetch')
    if 0 != code:
      raise RuntimeError('ERROR: "git fetch" failed.  See above.')
  checkoutCmd = 'git checkout %s' % gitRef
  code = run(checkoutCmd)
  if 0 != code:
    raise RuntimeError('ERROR: "%s" failed.  See above.' % checkoutCmd)
  if fetch:
    code = run('git pull')
    if 0 != code:
      raise RuntimeError('ERROR: "git pull" failed.  See above.')
  gitCheckoutSucceeded = True
  code = run('ant clean')
  if 0 != code:
    raise RuntimeError('ERROR: "ant clean" failed.  See above.')

def groupTestsByModule(tests):
  modules = {}
  for (dir, _, files) in os.walk('.'):
    for file in files:
      match = reJavaFile.search(file)
      if match is not None:
        test = match.group(1)
        if test in tests:
          match = reModule.match(dir)
          module = match.group(1)
          if module not in modules:
            modules[module] = set()
          modules[module].add(test)
  print('[repro] Test suites by module:')
  for module in modules:
    print('[repro]    %s' % module)
    for test in modules[module]:
      print('[repro]       %s' % test)
  return modules

def runTests(testIters, modules, tests):
  cwd = os.getcwd()
  testCmdline = 'ant test-nocompile -Dtests.dups=%d -Dtests.maxfailures=%d -Dtests.class="%s" -Dtests.showOutput=onerror %s'
  for module in modules:
    moduleTests = list(modules[module])
    testList = '|'.join(map(lambda t: '*.%s' % t, moduleTests))
    numTests = len(moduleTests)   
    params = tests[moduleTests[0]] # Assumption: all tests in this module have the same cmdline params
    os.chdir(module)
    code = run('ant compile-test')
    try:
      if 0 != code:
        raise RuntimeError("ERROR: Compile failed in %s/ with code %d.  See above." % (module, code))
      run(testCmdline % (testIters, testIters * numTests, testList, params))
    finally:
      os.chdir(cwd)
      
def printReport(testIters, location):
  failures = {}
  for start in ('lucene/build', 'solr/build'):
    for (dir, _, files) in os.walk(start):
      for file in files:
        testOutputFileMatch = reTestOutputFile.search(file)
        if testOutputFileMatch is not None:
          testcase = testOutputFileMatch.group(1)
          if testcase not in failures:
            failures[testcase] = 0
          with open(os.path.join(dir, file), encoding='UTF-8') as testOutputFile:
            for line in testOutputFile:
              errorFailureMatch = reErrorFailure.search(line)
              if errorFailureMatch is not None:
                failures[testcase] += 1
                break
  print("[repro] Failures%s:" % location)
  for testcase in sorted(failures, key=lambda t: (failures[t],t)): # sort by failure count, then by testcase 
    print("[repro]   %d/%d failed: %s" % (failures[testcase], testIters, testcase))
  return failures

def getLocalGitBranch():
  origGitBranch = runOutput('git rev-parse --abbrev-ref HEAD')
  if origGitBranch == 'HEAD':                       # In detached HEAD state
    origGitBranch = runOutput('git rev-parse HEAD') # Use the SHA when not on a branch
  print('[repro] Initial local git branch/revision: %s' % origGitBranch)
  return origGitBranch

def main():
  config = readConfig()
  tests = fetchAndParseJenkinsLog(config.url)
  localGitBranch = getLocalGitBranch()

  try:
    prepareWorkspace(config.fetch, revisionFromLog)
    modules = groupTestsByModule(tests)
    runTests(config.testIters, modules, tests)
    failures = printReport(config.testIters, '')
    
    # Retest 100% failures at the tip of the branch
    oldTests = tests
    tests = {}
    for fullClass in failures:
      testcase = fullClass[(fullClass.rindex('.') + 1):]
      if failures[fullClass] == config.testIters:
        tests[testcase] = oldTests[testcase]
    if len(tests) > 0:
      print('\n[repro] Re-testing 100%% failures at the tip of %s' % branchFromLog)
      prepareWorkspace(False, branchFromLog)
      modules = groupTestsByModule(tests)
      runTests(config.testIters, modules, tests)
      failures = printReport(config.testIters, ' at the tip of %s' % branchFromLog)
      
      # Retest 100% tip-of-branch failures without a seed
      oldTests = tests
      tests = {}
      for fullClass in failures:
        testcase = fullClass[(fullClass.rindex('.') + 1):]
        if failures[fullClass] == config.testIters:
          tests[testcase] = re.sub(reTestsSeed, '', oldTests[testcase])
      if len(tests) > 0:
        print('\n[repro] Re-testing 100%% failures at the tip of %s without a seed' % branchFromLog)
        prepareWorkspace(False, branchFromLog)
        modules = groupTestsByModule(tests)
        runTests(config.testIters, modules, tests)
        printReport(config.testIters, ' at the tip of %s without a seed' % branchFromLog)
  except Exception as e:
    print('[repro] %s' % traceback.format_exc())
    sys.exit(1)
  finally:
    if gitCheckoutSucceeded:
      run('git checkout %s' % localGitBranch, rememberFailure=False) # Restore original git branch/sha

  print('[repro] Exiting with code %d' % lastFailureCode)
  sys.exit(lastFailureCode)

if __name__ == '__main__':
  try:
    main()
  except KeyboardInterrupt:
    print('[repro] Keyboard interrupt...exiting')