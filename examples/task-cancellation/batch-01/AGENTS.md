# Demonstration contract

This is a synthetic, local-only demonstration. Do not access other projects or company information.
All delivered text must be English. Use Python 3.11+ standard library only.

The requested change is governed by `request.md`. Keep the service in memory and its API synchronous.
Cancellation is cooperative; do not claim Python threads can be forcibly terminated. Preserve public
method signatures and ordinary completion behavior. Do not add networking, persistence, dependencies,
retries, alternate providers, or fabricated review outputs.

Use bounded synchronization for tests, clean up worker threads, and exercise cancellation through
`TaskAPI`. Run `python3 -m unittest discover -s tests -v` and `python3 reproduce_cancel.py`.

`.scratch/` is ignored and reserved for temporary local work. Grounded Build runtime state must stay
outside this repository. Do not publish or push. Preserve raw workflow records outside public artifacts;
any public evidence export must be explicitly selected and checked for private paths and credentials.
