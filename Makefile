PYTHON ?= python3

.PHONY: all test bench

all:
	$(PYTHON) -c 'import prefix_tmpfs; print(prefix_tmpfs.build_binary())'

test:
	$(PYTHON) -m unittest discover -s tests -v

bench:
	$(PYTHON) benchmarks/small_files.py
