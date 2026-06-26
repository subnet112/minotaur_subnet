# Linking your GitHub account to your hotkey

To stop one miner submitting another miner's PR/code under their own hotkey, the
validator checks that the **fork owner** of a submission's PR is a GitHub account
**you have linked to your hotkey**. You link it once, with a hotkey-signed public
gist — nothing is ever committed to the solver repo.

## One-time registration

1. Build the canonical message for **your** GitHub login (lower-cased) and hotkey:

   ```
   MinotaurGithubLink:<your_github_login>:<your_hotkey_ss58>
   ```

2. Sign it with your hotkey (substrate `Keypair.sign`, hex output):

   ```python
   from bittensor_wallet.keypair import Keypair
   kp = Keypair.create_from_uri("//your-mnemonic-or-uri")
   msg = f"MinotaurGithubLink:{github_login.lower()}:{kp.ss58_address}"
   sig = "0x" + kp.sign(msg.encode()).hex()
   ```

3. Create a **public gist on the GitHub account you submit PRs from** with one file
   containing:

   ```json
   {"hotkey": "<your_hotkey_ss58>", "signature": "<sig from step 2>"}
   ```

4. Register the gist with the validator:

   ```
   POST /v1/miner/link-github
   {"gist_id": "<the gist id>"}
   ```

   The validator reads the gist **owner** from GitHub (authoritative — proving you
   control that account) and verifies the signature (proving you control the hotkey),
   then stores `github_login → hotkey`. Check it with `GET /v1/miner/identity/<login>`.

## Why this is safe

- The gist **owner** comes from GitHub, never from you — you can't claim an account
  whose gist you don't own.
- The **signature** is by your hotkey — nobody else can produce it.

So a copier can neither host a gist under your account nor forge your hotkey
signature. A submission whose PR fork owner isn't linked to the submitting hotkey is
rejected. (Copying the *code* into your own fork is a separate problem handled by
duplicate detection.)

## Notes

- GitHub logins are case-insensitive; the binding is stored lower-cased.
- Re-running the registration with a new gist re-links (e.g. to rotate the hotkey).
- The binding is persisted in the validator's database, so it **survives restarts** —
  you don't re-register each time the validator reboots.
