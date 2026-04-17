# This module has been removed.
#
# IdentityStore (the in-memory per-TargetManager dict[address → Identity])
# has been superseded by the IdentityQueue class in pool.py.  Identities are
# now stored in the shared Backend KV store under keys of the form
# ph:{target}:identity:{uuid} so that all HA instances share consistent state.
#
# See pool.py (IdentityQueue) and pool_store.py (IPPoolStore identity methods)
# for the replacement implementation.
