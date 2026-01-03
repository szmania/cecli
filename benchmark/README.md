# Aider benchmark harness

Before `cecli` was born, the old `aider` used benchmarks to quantitatively
measure how well it works with various LLMs.

This directory holds the harness and tools needed to run the benchmarking suite.

If you're familiar with the `aider` benchmarking, see the "What's new..."
section below.

## Background

The benchmark was based on the [Exercism](https://github.com/exercism/python)
coding exercises. This benchmark evaluates how effectively aider and LLMs can
translate a natural language coding request into executable code saved into
files that pass unit tests. It provides an end-to-end evaluation of not just the
LLM's coding ability, but also its capacity to _edit existing code_ and _format
those code edits_ so that aider can save the edits to the local source files.

See
[this writeup for a longer discussion about the benchmark](https://aider.chat/2024/12/21/polyglot.html).

The benchmark is intended to be run _inside a docker container_. This is because
the benchmarking harness will be taking code written by an LLM and executing it
without any human review or supervision! The LLM could generate dangerous python
that harms your system, like this: `import os; os.system("sudo rm -rf /")`.
Running inside a docker container helps limit the damage that could be done.

## Usage

There are 3 main tasks involved in benchmarking:

1. Install and setup.

2. Run the benchmark.

3. Analysis.

### Setup

These steps only need to be done once.

```
ORG=Aider-AI
REPO=aider
# Clone the main repo
git clone https://github.com/$ORG/$REPO.git

# Create the scratch dir to hold benchmarking results inside the main repo:
cd $REPO
mkdir tmp.benchmarks

# Clone the repo with the exercises
git clone https://github.com/$ORG/polyglot-benchmark tmp.benchmarks/polyglot-benchmark

# Build the docker container
./benchmark/docker_build.sh
```

### Running the benchmarks

Launch the docker container and run the benchmark inside it:

```
# Launch the docker container
# You probably want to tweak this script to import your service keys.
# It's currently configured to import GEMINI_API_KEY only.
# PR's welcome to more effectively grab the keys without causing anxiety.
./benchmark/docker.sh

# Inside the container, install aider as a development build.
# This way you're running the code that you cloned above, including any local changes.
# TODO: this step should be included in the Dockerfile
pip install -e .[dev]

# Run the benchmark:
./benchmark/benchmark.py a-helpful-name-for-this-run --model gpt-3.5-turbo --edit-format whole --threads 10 --exercises-dir polyglot-benchmark
```

The above will create a folder
`tmp.benchmarks/YYYY-MM-DD-HH-MM-SS--a-helpful-name-for-this-run` with
benchmarking results. Run like this, the script will run all the exercises in a
random order.

You can run `./benchmark/benchmark.py --help` for a list of all the arguments,
but here are the most useful to keep in mind:

- `--model` is the name of the model, same as you would pass directly to
  `aider`.
- `--edit-format` is the name of the edit format, same as you would pass
  directly to `aider`. When working with an experimental LLM, I recommend
  starting with `whole`
- `--sets` runs specific groups of tests using the `sets` in the `cat.yaml`.
  (Hopefully, the sets will grow with time but currently it just bookmarks
  the classic "polyglot" test battery.)
- `--hash-re` allows for deterministic slicing of the exercise set based on the
  exercise hash. This is useful for quickly grabbing a consistent subset or k-fold
  cross-validation. For example:
  - `^0`: 1/16 of the set.
  - `^[01]`: 1/8 of the set.
  - `^[0-3]`: 1/4 of the set.
  - `^.{2}[4-7]`: 1/4 of the set, using the 3 character of the hash.
- `--threads` specifies how many exercises to benchmark in parallel. Start with
  a single thread if you are working out the kinks on your benchmarking setup or
  working with a new model, etc. Once you are getting reliable results, you can
  speed up the process by running with more threads. 10 works well against the
  OpenAI APIs.
- `--num-tests` specifies how many of the tests to run before stopping. This is
  another way to start gently as you debug your benchmarking setup.
- `--keywords` filters the tests to run to only the ones whose name match the
  supplied argument (similar to `pytest -k xxxx`).
- `--read-model-settings=<filename.yml>` specify model settings, see here:
  https://aider.chat/docs/config/adv-model-settings.html#model-settings
- `--map-tokens` sets a token budget for the repo map sent with each request.
  Set `0` to disable the repo map. This lets you enable repo map usage for any
  model (e.g., `--map-tokens 1024`).

### Benchmark report

You can generate stats about any benchmark, including ones which are still
running. You don't need to run this inside the docker container, as it is just
collecting stats not executing unsafe python.

```
# Generate stats for a specific benchmarking directory
./benchmark/benchmark.py --stats tmp.benchmarks/YYYY-MM-DD-HH-MM-SS--a-helpful-name-for-this-run
```

The benchmark report is a yaml record with statistics about the run.

The key statistics are the `pass_rate_#` entries, which report the percent of
the tasks which had all tests passing. There will be multiple of these pass rate
stats, depending on the value of the `--tries` parameter.

The yaml also includes all the settings which were in effect for the benchmark
run. It also reports the git hash of the repo at the time that the benchmark was
run, with `(dirty)` if there were uncommitted changes. It's good practice to
commit the repo before starting a benchmark run. This way the `model`,
`edit_format` and `commit_hash` should be enough to reliably reproduce any
benchmark run.

## Contributing

Contributions of benchmark results and tests are welcome! Submit results by opening a PR.

Note the roadmap priorities:

1. Complete 'set up records' to support smart caching.
2. Atomic data collection. Most of the data is saved but need protocols for sharing.
3. **Dimensional Parameter Walking** allowing for n-dimensional parameter tuning,
   facilitating "gradient descent" approach to optimisation across multiple parameters.
   The test runner should accept n lists of options, e.g., ["thinking: 100", "thinking: 200", "thinking: 400"], ["optional: B", "optionD: C"].
4. Smart Caching so the runner can optionally skip any tests for which "similar" result data
   is already available based on fuzzy metadata matching. This aids iterative Testing as
   when adding a new option to a list of permutations, only the new permutations need to
   be run. Also when new Cats join the collection it is easy to incrementally collect the data.
5. Data aggregation and analysis. These will be separate specialised tools.

## Limitations

- These scripts are not intended for use by typical `cecli` end users.
- Some of the old (?deprecated) tools are written as `bash` scripts, so it will be hard to use
  them on Windows.
- Currently the JS and cpp tests appear broken.

## What's new with Cecli Cats?

The benchmark has evolved into a collection of **Cecli Atomic Tests (Cats)**.

- **YAML Metadata**: Every Cat has its own `cat.yaml` file containing metadata,
  including a unique UUID that may or may not be useful later.
- **Evolving Collection**: The directory structure of the Cats is laid out to
  facilitate the growth and evolution of the collection. As the benchmark
  matures, Cats will come and go.
- **Simplified Runner**: The test runner is being simplified to focus on its
  core job: executing tests and recording results. Downstream aggregation and
  analysis of results will be shifted to other tools and projects.
- **Subset Filtering**: see `--sets`
- **K-fold Evaluation Slicing**: The `--hash-re` option allows for deterministic
  slicing of the exercise (now `cats`) based on the exercise hash.
