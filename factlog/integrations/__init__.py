# SPDX-License-Identifier: Apache-2.0
"""Optional integrations that bridge external tools into a factlog KB.

Each integration is import-light: heavy third-party clients (e.g. pyzotero) are
imported lazily at run time, so ``import factlog`` stays cheap for users who
never touch an integration.
"""
