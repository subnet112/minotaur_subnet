# Minotaur solver-base image.
#
# The validator's screening pipeline builds miner-submitted Dockerfiles
# with `docker build --network=none` for security. That means miners
# CAN'T `pip install` from PyPI — everything they need must already be
# present in this base image, or they must vendor wheels into their repo.
#
# This image layers the common EVM-solver dependency set on top of the
# upstream ghcr.io/subnet112/solver-base:v1, which provides the crypto
# primitives (eth_abi, eth-hash, eth-utils, eth-typing), pandas/numpy,
# pydantic, and the python base.
#
# Versions are pinned to match the production champion image exactly
# (minotaur-genesis:v1 at the time this base was cut). Any dep the
# champion used at runtime MUST be installed here, else a network-free
# build of the same code will fail.

FROM ghcr.io/subnet112/solver-base:v1

# Upgrade + reinstall: we want a single "resolved" layer. The list is
# every dep the champion's minotaur-genesis:v1 carries that isn't in v1,
# with versions pinned. No `>=`, no floats — miner solvers must behave
# deterministically and a silent transitive bump could change scoring.
RUN pip install --no-cache-dir \
        aiohappyeyeballs==2.6.1 \
        aiohttp==3.13.5 \
        aiosignal==1.4.0 \
        attrs==26.1.0 \
        bitarray==3.8.1 \
        certifi==2026.2.25 \
        charset-normalizer==3.4.7 \
        ckzg==2.1.7 \
        eth-account==0.13.7 \
        eth-keyfile==0.8.1 \
        eth-keys==0.7.0 \
        eth-rlp==2.2.0 \
        frozenlist==1.8.0 \
        hexbytes==1.3.1 \
        idna==3.11 \
        multidict==6.7.1 \
        propcache==0.4.1 \
        pycryptodome==3.23.0 \
        pyunormalize==17.0.0 \
        requests==2.33.1 \
        rlp==4.1.0 \
        types-requests==2.33.0.20260408 \
        urllib3==2.6.3 \
        web3==7.15.0 \
        websockets==15.0.1 \
        yarl==1.23.0

# Smoke test baked into the image — catches partial installs or silent
# transitive regressions during our own build, before we tag. Removed
# after the test so the image doesn't carry test scaffolding.
COPY smoke_test.py /tmp/smoke_test.py
RUN python /tmp/smoke_test.py && rm /tmp/smoke_test.py
