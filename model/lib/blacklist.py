# The MIT License (MIT)
# Copyright © 2023 Nimble Labs LTD

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.
import time
import json
import wandb
import hashlib
import nimble as nb
from typing import Union, Tuple, Callable, List
from model.inference import Inference


async def is_request_in_cache(self, synapse: Inference) -> bool:
    # Hashes request
    # Note: Could be improved using a similarity check
    async with self.lock:
        request = json.dumps(list(synapse.messages))
        request_key = hashlib.sha256(request.encode()).hexdigest()
        current_block = self.metagraph.block

        should_blacklist: bool
        # Check if request is in cache, if not add it
        if request_key in self.request_cache:
            should_blacklist = True
        else:
            caller_hotkey = synapse.dendrite.hotkey
            self.request_cache[request_key] = current_block
            should_blacklist = False

        # Sanitize cache by removing old entries according to block span
        keys_to_remove = []
        for key, block in self.request_cache.items():
            if (
                block + self.config.miner.blacklist.request_cache_block_span
                < current_block
            ):
                keys_to_remove.append(key)

        for key in keys_to_remove:
            del self.request_cache[key]

    return should_blacklist


def default_blacklist(self, synapse: Inference) -> Union[Tuple[bool, str], bool]:
    # Check if the key is white listed.
    if synapse.dendrite.hotkey in self.config.miner.blacklist.whitelist:
        return False, "whitelisted hotkey"

    # Check if the key is black listed.
    if synapse.dendrite.hotkey in self.config.miner.blacklist.blacklist:
        return True, "blacklisted hotkey"

    # Check registration if we do not allow non-registered users
    if (
        not self.config.miner.blacklist.allow_non_registered
        and self.metagraph is not None
        and synapse.dendrite.hotkey not in self.metagraph.hotkeys
    ):
        return True, "hotkey not registered"

    # Check if the key has validator permit
    if self.config.miner.blacklist.force_validator_permit:
        if synapse.dendrite.hotkey in self.metagraph.hotkeys:
            uid = self.metagraph.hotkeys.index(synapse.dendrite.hotkey)
            if not self.metagraph.validator_permit[uid]:
                return True, "validator permit required"
        else:
            return True, "validator permit required, but hotkey not registered"

    # request period
    if synapse.dendrite.hotkey in self.request_timestamps:
        period = time.time() - self.request_timestamps[synapse.dendrite.hotkey][0]
        if period < self.config.miner.blacklist.min_request_period * 60:
            return (
                True,
                f"{synapse.dendrite.hotkey} request frequency exceeded {len(self.request_timestamps[synapse.dendrite.hotkey])} requests in {self.config.miner.blacklist.min_request_period} minutes.",
            )

    # Otherwise the user is not blacklisted.
    return False, "passed blacklist"


def blacklist(
    self, func: Callable, synapse: Inference
) -> Union[Tuple[bool, str], bool]:
    nb.logging.trace("run blacklist function")

    # First check to see if the black list function is overridden by the subclass.
    does_blacklist = None
    reason = None
    try:
        # Run the subclass blacklist function.
        blacklist_result = func(synapse)

        # Unpack result.
        if hasattr(blacklist_result, "__len__"):
            does_blacklist, reason = blacklist_result
        else:
            does_blacklist = blacklist_result
            reason = "no reason provided"

    except NotImplementedError:
        # The subclass did not override the blacklist function.
        does_blacklist, reason = default_blacklist(self, synapse)

    except Exception as e:
        # There was an error in their blacklist function.
        nb.logging.error(f"Error in blacklist function: {e}")
        does_blacklist, reason = default_blacklist(self, synapse)

    finally:
        # If the blacklist function returned None, we use the default blacklist.
        if does_blacklist == None:
            does_blacklist, reason = default_blacklist(self, synapse)

        # Finally, log and return the blacklist result.
        nb.logging.trace(f"blacklisted: {does_blacklist}, reason: {reason}")
        if does_blacklist and self.config.wandb.on:
            wandb.log(
                {
                    "blacklisted": float(does_blacklist),
                    "return_message": reason,
                    "hotkey": synapse.dendrite.hotkey,
                }
            )
        return does_blacklist, reason
