# pytest-overwatch

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**pytest-overwatch** is a Jest inspired interactive test runner plugin for
pytest. It reruns tests whenever files change and allows you to select a
subset of tests to run.

**Work in progress!**

## Features

- [x] re-run tests on file change
- [x] select subset of tests based on filename
- [ ] select subset of tests based on test name
- [x] support dropping into debugger on test failure

## In action

![](extras/demo.gif)

## Motivation

**pytest** is my go-to test runner for python projects and I use it heavily at
work. I usually use the `--looponfailure` feature of the
**pytest-xdist** plugin on the side, however having worked quite some time
with Jest in the javascript world, I was missing two things:

* ability to rerun all the selected tests constantly - instead of just the
  failed ones - to discover potential new failures
* running a subset of all tests easily

## Related projects

* https://github.com/pytest-dev/pytest-xdist
* https://github.com/joeyespo/pytest-watch
* https://github.com/facebook/jest