# Copyright 2020 Adap GmbH. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Flower server app."""


import argparse
from dataclasses import dataclass
from logging import INFO, WARN
from typing import Optional, Tuple

import uvicorn

from flwr.common import GRPC_MAX_MESSAGE_LENGTH
from flwr.common.logger import log
from flwr.proto.driver_pb2_grpc import add_DriverServicer_to_server
from flwr.proto.transport_pb2_grpc import add_FlowerServiceServicer_to_server
from flwr.server.client_manager import ClientManager, SimpleClientManager
from flwr.server.driver.driver_client_manager import DriverClientManager
from flwr.server.driver.driver_servicer import DriverServicer
from flwr.server.driver.state import DriverState
from flwr.server.grpc_server.flower_service_servicer import FlowerServiceServicer
from flwr.server.grpc_server.grpc_server import (
    generic_create_grpc_server,
    start_grpc_server,
)
from flwr.server.history import History
from flwr.server.server import Server
from flwr.server.strategy import FedAvg, Strategy

DEFAULT_GRPC_SERVER_ADDRESS = "[::]:8080"
DEFAULT_REST_SERVER_HOST = "0.0.0.0"
DEFAULT_REST_SERVER_PORT = 8000
DEFAULT_SERVER_ADDRESS_DRIVER = "[::]:9091"
DEFAULT_SERVER_ADDRESS_FLEET = "[::]:9092"


@dataclass
class ServerConfig:
    """Flower server config.

    All attributes have default values which allows users to configure
    just the ones they care about.
    """

    num_rounds: int = 1
    round_timeout: Optional[float] = None


def start_server(  # pylint: disable=too-many-arguments
    *,
    server_address: str = DEFAULT_GRPC_SERVER_ADDRESS,
    server: Optional[Server] = None,
    config: Optional[ServerConfig] = None,
    strategy: Optional[Strategy] = None,
    client_manager: Optional[ClientManager] = None,
    grpc_max_message_length: int = GRPC_MAX_MESSAGE_LENGTH,
    certificates: Optional[Tuple[bytes, bytes, bytes]] = None,
) -> History:
    """Start a Flower server using the gRPC transport layer.

    Parameters
    ----------
    server_address : Optional[str]
        The IPv4 or IPv6 address of the server. Defaults to `"[::]:8080"`.
    server : Optional[flwr.server.Server] (default: None)
        A server implementation, either `flwr.server.Server` or a subclass
        thereof. If no instance is provided, then `start_server` will create
        one.
    config : Optional[ServerConfig] (default: None)
        Currently supported values are `num_rounds` (int, default: 1) and
        `round_timeout` in seconds (float, default: None).
    strategy : Optional[flwr.server.Strategy] (default: None).
        An implementation of the abstract base class
        `flwr.server.strategy.Strategy`. If no strategy is provided, then
        `start_server` will use `flwr.server.strategy.FedAvg`.
    client_manager : Optional[flwr.server.ClientManager] (default: None)
        An implementation of the abstract base class
        `flwr.server.ClientManager`. If no implementation is provided, then
        `start_server` will use
        `flwr.server.client_manager.SimpleClientManager`.
    grpc_max_message_length : int (default: 536_870_912, this equals 512MB)
        The maximum length of gRPC messages that can be exchanged with the
        Flower clients. The default should be sufficient for most models.
        Users who train very large models might need to increase this
        value. Note that the Flower clients need to be started with the
        same value (see `flwr.client.start_client`), otherwise clients will
        not know about the increased limit and block larger messages.
    certificates : Tuple[bytes, bytes, bytes] (default: None)
        Tuple containing root certificate, server certificate, and private key
        to start a secure SSL-enabled server. The tuple is expected to have
        three bytes elements in the following order:

            * CA certificate.
            * server certificate.
            * server private key.

    Returns
    -------
    hist : flwr.server.history.History
        Object containing training and evaluation metrics.

    Examples
    --------
    Starting an insecure server:

    >>> start_server()

    Starting an SSL-enabled server:

    >>> start_server(
    >>>     certificates=(
    >>>         Path("/crts/root.pem").read_bytes(),
    >>>         Path("/crts/localhost.crt").read_bytes(),
    >>>         Path("/crts/localhost.key").read_bytes()
    >>>     )
    >>> )
    """

    # Initialize server and server config
    initialized_server, initialized_config = _init_defaults(
        server=server,
        config=config,
        strategy=strategy,
        client_manager=client_manager,
    )
    log(
        INFO,
        "Starting Flower server, config: %s",
        initialized_config,
    )

    # Start gRPC server
    grpc_server = start_grpc_server(
        client_manager=initialized_server.client_manager(),
        server_address=server_address,
        max_message_length=grpc_max_message_length,
        certificates=certificates,
    )
    log(
        INFO,
        "Flower ECE: gRPC server running (%s rounds), SSL is %s",
        initialized_config.num_rounds,
        "enabled" if certificates is not None else "disabled",
    )

    # Start training
    hist = _fl(
        server=initialized_server,
        config=initialized_config,
    )

    # Stop the gRPC server
    grpc_server.stop(grace=1)

    return hist


def _init_defaults(
    server: Optional[Server],
    config: Optional[ServerConfig],
    strategy: Optional[Strategy],
    client_manager: Optional[ClientManager],
) -> Tuple[Server, ServerConfig]:
    # Create server instance if none was given
    if server is None:
        if client_manager is None:
            client_manager = SimpleClientManager()
        if strategy is None:
            strategy = FedAvg()
        server = Server(client_manager=client_manager, strategy=strategy)
    elif strategy is not None:
        log(WARN, "Both server and strategy were provided, ignoring strategy")

    # Set default config values
    if config is None:
        config = ServerConfig()

    return server, config


def _fl(
    server: Server,
    config: ServerConfig,
) -> History:
    # Fit model
    hist = server.fit(num_rounds=config.num_rounds, timeout=config.round_timeout)
    log(INFO, "app_fit: losses_distributed %s", str(hist.losses_distributed))
    log(INFO, "app_fit: metrics_distributed %s", str(hist.metrics_distributed))
    log(INFO, "app_fit: losses_centralized %s", str(hist.losses_centralized))
    log(INFO, "app_fit: metrics_centralized %s", str(hist.metrics_centralized))

    # Graceful shutdown
    server.disconnect_all_clients(timeout=config.round_timeout)

    return hist


def run_server() -> None:
    """Run Flower server."""
    args = parse_args()

    log(INFO, "Starting Flower server")

    driver_state = DriverState()
    driver_client_manager = DriverClientManager(
        driver_state=driver_state,
    )

    log(INFO, "Starting Flower gRPC server")

    # Create Driver API gRPC server
    driver_server_address: str = DEFAULT_SERVER_ADDRESS_DRIVER
    driver_servicer = DriverServicer(
        driver_client_manager=driver_client_manager,
        driver_state=driver_state,
    )
    driver_add_servicer_to_server_fn = add_DriverServicer_to_server
    driver_grpc_server = generic_create_grpc_server(
        servicer_and_add_fn=(driver_servicer, driver_add_servicer_to_server_fn),
        server_address=driver_server_address,
        max_message_length=GRPC_MAX_MESSAGE_LENGTH,
        certificates=None,
    )

    # Start Fleet API (gRPC-based)
    fleet_grpc_server = None
    if args.server_type == "grpc":
        # Create (legacy) Fleet API gRPC server
        fleet_server_address: str = DEFAULT_SERVER_ADDRESS_FLEET
        fleet_servicer = FlowerServiceServicer(
            client_manager=driver_client_manager,
        )
        fleet_add_servicer_to_server_fn = add_FlowerServiceServicer_to_server
        fleet_grpc_server = generic_create_grpc_server(
            servicer_and_add_fn=(fleet_servicer, fleet_add_servicer_to_server_fn),
            server_address=fleet_server_address,
            max_message_length=GRPC_MAX_MESSAGE_LENGTH,
            certificates=None,
        )

    # Start Fleet API (HTTP-based)
    elif args.server_type == "rest":
        port = args.rest_bind_port
        host = args.rest_bind_host
        log(INFO, "Starting Flower REST server")
        uvicorn.run(
            "flwr.server.rest_server.rest_api:app",
            port=port,
            host=host,
            reload=True,
            access_log=False,
        )

    # Start Driver API gRPC server
    driver_grpc_server.start()
    log(
        INFO,
        "Flower ECE: driver gRPC server running on %s",
        driver_server_address,
    )

    # Start (legacy) Fleet API gRPC server
    if fleet_grpc_server:
        fleet_grpc_server.start()
        log(
            INFO,
            "Flower ECE: fleet gRPC server running on %s",
            fleet_server_address,
        )

    # Wait for termination of both servers
    driver_grpc_server.wait_for_termination()
    if fleet_grpc_server:
        fleet_grpc_server.wait_for_termination()
    # TODO handle REST server termination


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Use this script to start a Flower Server."
    )
    # Possible Server types
    ex_g = parser.add_mutually_exclusive_group()
    ex_g.add_argument(
        "--grpc",
        action="store_const",
        dest="server_type",
        const="grpc",
        default="grpc",
        help="Starts a gRPC Flower server",
    )
    ex_g.add_argument(
        "--rest",
        action="store_const",
        dest="server_type",
        const="rest",
        help="Starts a REST Flower server",
    )
    # Possible options
    grpc_group = parser.add_argument_group("gRPC Server options", "")
    grpc_group.add_argument(
        "--grpc_driver_address",
        help=f"Driver server address. Default:'{DEFAULT_SERVER_ADDRESS_DRIVER}'",
        default=DEFAULT_SERVER_ADDRESS_DRIVER,
    )
    grpc_group.add_argument(
        "--grpc_fleet_address",
        help=f"Fleet server address. Default:'{DEFAULT_SERVER_ADDRESS_FLEET}'",
        default=DEFAULT_SERVER_ADDRESS_FLEET,
    )
    grpc_group.add_argument(
        "--grpc_server_address",
        help=f"gRPC server address [DEPRECATED]. Default:'{DEFAULT_GRPC_SERVER_ADDRESS}'",
        default=DEFAULT_GRPC_SERVER_ADDRESS,
    )
    rest_group = parser.add_argument_group("REST Server options", "")
    rest_group.add_argument(
        "--rest_bind_host",
        help=f"REST bind socket to this host. Default:'{DEFAULT_REST_SERVER_HOST}'",
        default=DEFAULT_REST_SERVER_HOST,
    )
    rest_group.add_argument(
        "--rest_bind_port",
        help=f"REST bind to a socket with this port. Default:'{DEFAULT_REST_SERVER_PORT}'",
        default=DEFAULT_REST_SERVER_PORT,
    )
    return parser.parse_args()
