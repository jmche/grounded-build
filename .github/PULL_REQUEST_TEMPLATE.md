## Problem and evidence

Describe the observable defect or capability and provide a minimal reproduction.

## Root cause and solution

Explain the affected state, authority, or trust boundary and why this change is sufficient.

## Verification

- [ ] Added or updated regression tests
- [ ] `python3 scripts/release_check.py`
- [ ] `git diff --check`
- [ ] No paid provider calls were used by the test suite

## Compatibility and risk

- [ ] State or engine-contract migration impact is documented
- [ ] Security and credential exposure were considered
- [ ] Provider/model behavior remains accurately represented
- [ ] Documentation and changelog are updated when public behavior changed
