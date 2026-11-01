# Contributing

Thank you for investing your time in contributing to Mol-1!

Read our [Code of Conduct](./CODE_OF_CONDUCT.md) to keep our community approachable and respectable.

This guide covers how to use issues and pull requests to improve Mol-1.

## General Guidelines

### Pull Requests

Make sure to keep Pull Requests small and functional to make them easier to review, understand, and look up in commit history. This repository uses "Squash and merge" to keep our history clean and make it easier to revert changes based on PR.

Adding the appropriate documentation, unit tests and e2e tests as part of a feature is the responsibility of the feature owner, whether it is done in the same Pull Request or not.

Pull Requests should follow the "subject: message" format, where the subject describes what part of the code is being modified.

Refer to the template for more information on what goes into a PR description.

### Design Docs

A contributor proposes a design with a PR on the repository to allow for revisions and discussions. If a design needs to be discussed before formulating a document for it, make use of Google doc and GitHub issue to involve the community on the discussion. 

### GitHub Issues

GitHub Issues are used to file bugs, work items, and feature requests with actionable items/issues (Please refer to the "Reporting Bugs/Feature Requests" section below for more information).

### Reporting Bugs/Feature Requests

We welcome you to use the GitHub issue tracker to report bugs or suggest features that have actionable items/issues (as opposed to introducing a feature request on GitHub Discussions).

When filing an issue, please check existing open, or recently closed, issues to make sure somebody else hasn't already reported the issue. Please try to include as much information as you can. Details like these are incredibly useful:

- A reproducible test case or series of steps
- The version of the code being used
- Any modifications you've made relevant to the bug
- Anything unusual about your environment or deployment

## Contributing via Pull Requests

### Find interesting issue

If you spot a problem, search whether an issue already exists in this repository's issue tracker. If none does, open a new one.

### Solve an issue

[docs/installation.md](docs/installation.md) covers setting up an environment,
and the [README](README.md) covers running the model. The test suite runs with:

```bash
pip install -e ".[dev]"
pytest tests/
```

Tests that need a GPU, a network service or an optional kernel dependency skip
themselves when it is unavailable, so a CPU-only run should report skips rather
than failures. Tests reaching external services carry the `network` marker and
can be excluded with `-m "not network"`.

### Copyright headers

Mol-1 is derived from [Protenix](https://github.com/bytedance/Protenix)
(Apache-2.0), so a file's header depends on where the file came from:

- **New files**, and files with no upstream code left, carry the Molence Bio
  copyright and the Apache-2.0 license text.
- **Files modified from upstream** keep their original copyright notice and add
  a `Modifications Copyright <year> Molence Bio` line, a note that the file was
  modified, and the upstream revision and path.
- **Files reused unchanged** keep their original header as it is.

Copy the header from an existing file of the same kind.

### Open a Pull request.

When you're done making the changes, open a pull request and fill PR template so we can better review your PR. The template helps reviewers understand your changes and the purpose of your pull request. 

Don't forget to link PR to the issue if you are solving one.

If you run into any merge issues, checkout this [git tutorial](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/addressing-merge-conflicts) to help you resolve merge conflicts and other issues.


## Finding contributions to work on

Looking at the existing issues is a great way to find something to contribute on. As our projects, by default, use the default GitHub issue labels (enhancement/bug/duplicate/help wanted/invalid/question/wontfix), looking at any 'help wanted' and 'good first issue' issues are a great place to start.
