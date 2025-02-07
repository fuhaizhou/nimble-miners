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

import copy
import wandb
import asyncio
import argparse
import threading

from abc import ABC, abstractmethod
from typing import List, Dict, Tuple, Union

import nimble as nb
from model.inference import Inference

from model.lib.priority import priority
from model.lib.blacklist import blacklist, is_request_in_cache
from model.lib.run import run
from model.lib.set_weights import set_weights
from model.lib.config import check_config, get_config


class Miner(ABC):
    """
    The Miner class is an abstract base class that defines the structure for Nimble miners.
    Subclasses should implement the `predict` method to define their own response logic.
    The `blacklist` and `priority` methods can also be overridden to provide custom logic.
    """

    def __init__(self, config=None, axon=None, wallet=None, nbnetwork=None):
        """
        Initializes the Miner with the given configurations and Nimble objects.

        Args:
            config: Configuration object that holds settings for the miner.
            axon: Nimble Axon object which handles incoming requests.
            wallet: Nimble Wallet object which holds cryptographic keys.
            nbnetwork: Nimble Subtensor object which manages the blockchain connection.
        """
        # Setup base config from Miner.config() and merge with subclassed config.
        base_config = copy.deepcopy(config or get_config())
        self.config = self.config()
        self.config.merge(base_config)

        check_config(Miner, self.config)
        nb.logging.info(self.config)  # TODO: duplicate print?

        self.request_cache: Dict[str, Tuple[str, int]] = {}

        # Activating Nimble's logging with the set configurations.
        nb.logging(config=self.config, logging_dir=self.config.full_path)

        if not self.config.miner.blacklist.force_validator_permit:
            nb.logging.warning(
                "You are allowing non-validators to send requests to your miner. This is a security risk."
            )
        if self.config.miner.blacklist.allow_non_registered:
            nb.logging.warning(
                "You are allowing non-registered entities to send requests to your miner. This is a security risk. "
            )

        nb.logging.info("Setting up nimble objects.")

        # Wallet holds cryptographic information, ensuring secure transactions and communication.
        self.wallet = wallet or nb.wallet(config=self.config)
        nb.logging.info(f"Wallet {self.wallet}")

        # nbnetwork manages the blockchain connection, facilitating interaction with the Nimble blockchain.
        self.nbnetwork = nbnetwork or nb.nbnetwork(config=self.config)
        nb.logging.info(f"NBNetwork: {self.nbnetwork}")
        nb.logging.info(
            f"Running miner for subnet: {self.config.netuid} on network: {self.nbnetwork.chain_endpoint} with config:"
        )

        # metagraph provides the network's current state, holding state about other participants in a subnet.
        self.metagraph = self.nbnetwork.metagraph(self.config.netuid)
        nb.logging.info(f"Metagraph: {self.metagraph}")

        if self.wallet.hotkey.ss58_address not in self.metagraph.hotkeys:
            nb.logging.error(
                f"\nYour validator: {self.wallet} if not registered to chain connection: {self.nbnetwork} \nRun nbcli subnets register and try again. "
            )
            exit()
        else:
            # Each miner gets a unique identity (UID) in the network for differentiation.
            self.my_subnet_uid = self.metagraph.hotkeys.index(
                self.wallet.hotkey.ss58_address
            )
            nb.logging.info(f"Running miner on uid: {self.my_subnet_uid}")

        # The axon handles request processing, allowing validators to send this process requests.
        self.axon = axon or nb.axon(
            wallet=self.wallet,
            port=self.config.axon.port,
            external_ip=self.config.axon.external_ip,
        )
        # Attach determiners which functions are called when servicing a request.
        nb.logging.info(f"Attaching forward function to axon.")
        self.axon.attach(
            forward_fn=self._predict,
            blacklist_fn=self.blacklist,
            priority_fn=self.priority,
        )
        nb.logging.info(f"Axon created: {self.axon}")

        if self.config.wandb.on:
            tags = [self.wallet.hotkey.ss58_address, f"netuid_{self.config.netuid}"]
            self.wandb_run = wandb.init(
                project=self.config.wandb.project_name,
                entity=self.config.wandb.entity,
                config=self.config,
                mode="online" if self.config.wandb.on else "offline",
                dir=self.config.miner.full_path,
                magic=True,
                tags=tags,
            )

        # Instantiate runners
        self.should_exit: bool = False
        self.is_running: bool = False
        self.thread: threading.Thread = None
        self.lock = asyncio.Lock()
        self.request_timestamps: Dict = {}

    @abstractmethod
    def config(self) -> "nb.Config":
        """
        Abstract method for configuring the Miner.

        Subclasses should implement this method to return a configuration object that dictates
        various settings and parameters for the miner's operation. The returned configuration
        object will typically contain parameters like network settings, logging preferences,
        and other operational parameters.

        Returns:
            nb.Config: A configuration object specific to the miner subclass.
        """
        ...

    @classmethod
    @abstractmethod
    def add_args(cls, parser: argparse.ArgumentParser):
        """
        Abstract class method to add miner-specific arguments to a command line parser.

        This method should be implemented by subclasses to introduce any command-line
        arguments that the miner might require for operation.

        Args:
            parser (argparse.ArgumentParser): The command line argument parser to which
                the miner-specific arguments should be added.
        """
        ...

    def _predict(self, synapse: Inference) -> Inference:
        """
        A wrapper method around the `predict` method that will be defined by the subclass.

        This method acts as an intermediary layer to perform pre-processing before calling the
        actual `predict` method implemented in the subclass. Specifically, it checks whether a
        prediction request is in cache to avoid reprocessing recent requests. If the predict is not in the
        cache, the subclass `predict` method is called.

        Args:
            synapse (Inference): The incoming request object encapsulating the details of the request.

        Returns:
            Inference: The response object to be sent back in reply to the incoming request, essentially
            the filled synapse request object.

        Raises:
            ValueError: If the request is found in the cache indicating it was sent recently.

        Example:
            This method is not meant to be called directly but is invoked internally when a request
            is received, and it subsequently calls the `request` method of the subclass.
        """
        if self.config.miner.blacklist.use_request_cache:
            if is_request_in_cache(self, synapse):
                raise ValueError(
                    f"Blacklisted: Request sent recently in last {self.config.miner.blacklist.request_cache_block_span} blocks."
                )
        return self.predict(synapse)

    @abstractmethod
    def predict(self, synapse: Inference) -> Inference:
        """
        Abstract method to handle and respond to incoming requests to the miner.

        Subclasses should implement this method to define their custom logic for processing and
        responding to requests. This method is designed to be overridden, and its behavior will
        be dependent on the specific implementation provided in the subclass.

        Args:
            synapse (Inference): The incoming request object encapsulating the details
                of the request. This must contain `messages` and `roles` as fields.

        Returns:
            Inference: The response object that should be sent back in reply to the
                incoming request. This is essentially the filled synapse request object.

        Example:
            class CustomMiner(Miner):
                def predict(self, synapse: Inference) -> Inference:
                    # Custom logic to process and respond to the request.
                    synapse.completion = "The meaning of life is 42."
                    return synapse
        """
        ...

    def blacklist(self, synapse: Inference) -> Tuple[bool, str]:
        """
        Default blacklist logic

        Define how miners should blacklist requests. This Function
        Runs before the synapse data has been deserialized (i.e. before synapse.data is available).
        The synapse is instead contructed via the headers of the request. It is important to blacklist
        requests before they are deserialized to avoid wasting resources on requests that will be ignored.

        Below: Check that the hotkey is a registered entity in the metagraph.

        Args:
            synapse (:obj:`nimble.synapse.Synapse`, `required`):
                synapse object containing the request headers.
        Returns:
            blacklisted (:obj:`bool`):
        """

        def _blacklist(synapse: "Inference") -> Tuple[bool, str]:
            raise NotImplementedError("blacklist not implemented in subclass")

        return blacklist(self, _blacklist, synapse)

    def priority(self, synapse: Inference) -> float:
        """
        Define how miners should prioritize requests.

        Miners may recieve messages from multiple entities at once. This function
        determines which request should be processed first. Higher values indicate
        that the request should be processed first. Lower values indicate that the
        request should be processed later.

        Below: simple logic, prioritize requests from entities with more stake.

        Args:
            synapse (:obj:`nimble.synapse.Synapse`, `required`):
                synapse object containing the request headers.
        Returns:
            priority (:obj:`float`):
        """

        def _priority(synapse: "Inference") -> bool:
            raise NotImplementedError("priority not implemented in subclass")

        return priority(self, _priority, synapse)

    def run(self):
        """
        Runs the miner logic. This method starts the miner's operations, including
        listening for incoming requests and periodically updating the miner's knowledge
        of the network graph.
        """
        run(self)

    def run_in_background_thread(self):
        """
        Starts the miner's operations in a separate background thread.
        This is useful for non-blocking operations.
        """
        if not self.is_running:
            nb.logging.debug("Starting miner in background thread.")
            self.should_exit = False
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()
            self.is_running = True
            nb.logging.debug("Started")

    def stop_run_thread(self):
        """
        Stops the miner's operations that are running in the background thread.
        """
        if self.is_running:
            nb.logging.debug("Stopping miner in background thread.")
            self.should_exit = True
            self.thread.join(5)
            self.is_running = False
            nb.logging.debug("Stopped")

    def __enter__(self):
        """
        Starts the miner's operations in a background thread upon entering the context.
        This method facilitates the use of the miner in a 'with' statement.
        """
        self.run_in_background_thread()

    def __exit__(self, exc_type, exc_value, traceback):
        """
        Stops the miner's background operations upon exiting the context.
        This method facilitates the use of the miner in a 'with' statement.

        Args:
            exc_type: The type of the exception that caused the context to be exited.
                      None if the context was exited without an exception.
            exc_value: The instance of the exception that caused the context to be exited.
                       None if the context was exited without an exception.
            traceback: A traceback object encoding the stack trace.
                       None if the context was exited without an exception.
        """
        self.stop_run_thread()
