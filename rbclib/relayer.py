import json

from src.chainpy.eth.ethtype.amount import EthAmount
from src.chainpy.eth.ethtype.hexbytes import EthAddress, EthHashBytes
from src.chainpy.eventbridge.eventbridge import EventBridge
from src.chainpy.eth.managers.configs import EntityRootConfig
from src.chainpy.eth.ethtype.consts import ChainIndex
from src.chainpy.eth.ethtype.account import EthAccount
from src.chainpy.eventbridge.multichainmonitor import bootstrap_logger
from src.rbcevents.consts import ConsensusOracleId, TokenStreamIndex, AggOracleId
from src.rbcevents.periodicevents import BtcHashUpOracle, AuthDownOracle, PriceUpOracle
import time

from utils import RELAYER_VERSION

BIFROST_VALIDATOR_HISTORY_LIMIT_BLOCKS = 6
BOOTSTRAP_OFFSET_ROUNDS = 5


class Relayer(EventBridge):
    def __init__(self, entity_config: EntityRootConfig, relayer_index_cache_max_length: int = 100):
        super().__init__(entity_config, int, relayer_index_cache_max_length)
        self.current_rnd = None

    @property
    def valid_min_rnd(self):
        if self.current_rnd is None:
            raise Exception("relayer's current rnd is None")
        return self.current_rnd - BIFROST_VALIDATOR_HISTORY_LIMIT_BLOCKS

    @classmethod
    def init_from_config_files(cls,
                               relayer_config_path: str,
                               private_config_path: str = None,
                               private_key: str = None,
                               pem_path: str = None,
                               password: str = None):
        if pem_path is not None and password is None:
            raise Exception("Pem file requires a password")

        with open(relayer_config_path, "r") as f:
            relayer_config_dict = json.load(f)

        private_config_dict = None
        if private_config_path is not None:
            with open(private_config_path, "r") as f:
                private_config_dict = json.load(f)

        return cls.init_from_dicts(
            relayer_config_dict,
            private_config_dict=private_config_dict,
            private_key=private_key,
            pem_path=pem_path,
            password=password
        )

    @classmethod
    def init_from_dicts(cls,
                        relayer_config_dict: dict,
                        private_config_dict: dict = None,
                        private_key: str = None,
                        pem_path: str = None,
                        password: str = None):
        root_config: EntityRootConfig = EntityRootConfig.from_dict(relayer_config_dict, private_config_dict)

        if private_key is not None:
            root_config.entity.secret_hex = EthHashBytes(private_key).hex()

        if pem_path is not None:
            with open(pem_path, "r") as f:
                lines = f.readlines()
            decoded_pem = "".join(lines)
            account: EthAccount = EthAccount.from_private_key_pem(decoded_pem.encode(), password)
            root_config.entity.secret_hex = hex(account.priv)

        Relayer.init_classes(root_config)
        return cls(root_config)

    @staticmethod
    def init_classes(root_config: EntityRootConfig):
        # setup hardcoded value (not from config file) because it's a system parameter
        AuthDownOracle.setup(60)

        price_oracle_config = root_config.oracle_config.asset_prices
        PriceUpOracle.setup(
            price_oracle_config.names,
            price_oracle_config.source_names,
            price_oracle_config.urls,
            price_oracle_config.collection_period_sec
        )

        btc_hash_oracle_config = root_config.oracle_config.bitcoin_block_hash
        BtcHashUpOracle.setup(
            btc_hash_oracle_config.url,
            btc_hash_oracle_config.auth_id,
            btc_hash_oracle_config.auth_password,
            btc_hash_oracle_config.collection_period_sec
        )

    def find_height_by_timestamp(self, chain_index: ChainIndex, target_time: int, front_height: int = 0, front_time: int = 0):
        chain_manager = self.get_chain_manager_of(chain_index)
        current_block = chain_manager.eth_get_block_by_height()

        current_height, current_time = current_block.number, current_block.timestamp  # as a rear

        if front_height < 1:
            front_height, front_time = chain_manager.latest_height, chain_manager.eth_get_block_by_height(chain_manager.latest_height).timestamp

        if front_time >= target_time:
            return front_height

        if chain_index != ChainIndex.BIFROST:
            target_time -= 30000
        return self._binary_search(chain_index, front_height, front_time, current_height, current_time, target_time)

    def _binary_search(self,
                       chain_index: ChainIndex,
                       front_height: int, front_time: int,
                       rear_height: int, rear_time: int,
                       target_time: int) -> int:
        if front_time > rear_time or front_height > rear_height:
            raise Exception("binary search prams error: front > rear")

        medium_height = (front_height + rear_height) // 2
        medium_block = self.get_chain_manager_of(chain_index).eth_get_block_by_height(medium_height)
        if abs(target_time - medium_block.timestamp) < 30000:  # 30 secs
            return medium_height
        elif target_time > medium_block.timestamp:
            return self._binary_search(
                chain_index,
                medium_height, medium_block.timestamp,
                rear_height, rear_time,
                target_time
            )
        else:
            return self._binary_search(
                chain_index,
                front_height, front_time,
                medium_height, medium_block.timestamp,
                target_time
            )

    def _register_auth(self, rnd: int, relayer_addr_list: list, addr: str):
        addr_lower = addr.lower()
        relayer_lower_list = [relayer_addr.lower() for relayer_addr in relayer_addr_list]
        sorted_relayer_list = sorted(relayer_lower_list)

        try:
            relayer_index = sorted_relayer_list.index(addr_lower)
            self.set_value_by_key(rnd, relayer_index)
        except ValueError:
            pass

    def register_relayer_index(self, rnd: int):
        my_addr = self.active_account.address.hex().lower()
        sorted_validator_list = self.fetch_sorted_previous_validator_list(ChainIndex.BIFROST, rnd)
        self._register_auth(rnd, sorted_validator_list, my_addr)

    def run_relayer(self):
        while True:
            # wait node's block synchronization
            chain_manager = self.get_chain_manager_of(ChainIndex.BIFROST)
            try:
                result = chain_manager.send_request("system_health", [])["isSyncing"]
            except Exception as e:
                time.sleep(10)
                continue

            if not result:
                break
            else:
                print(">>> BIFROST Node is syncing..")
                time.sleep(60)

        # check whether this relayer belongs to current validator list
        self.current_rnd = self.fetch_validator_round(ChainIndex.BIFROST)
        round_history_limit = min(BIFROST_VALIDATOR_HISTORY_LIMIT_BLOCKS, self.current_rnd)
        for i in range(round_history_limit):
            self.register_relayer_index(self.current_rnd - i)

        current_block, current_rnd_idx, round_length = self.fetch_validator_round_info()
        target_block_num = current_block - round_length * BOOTSTRAP_OFFSET_ROUNDS
        target_block_num = max(target_block_num, 1)

        target_block = self.get_chain_manager_of(ChainIndex.BIFROST).eth_get_block_by_height(target_block_num)
        target_time = target_block.timestamp

        for chain_index in self.supported_chain_list:
            chain_manager = self.get_chain_manager_of(chain_index)
            if chain_index == ChainIndex.BIFROST:
                chain_manager.latest_height = target_block_num
            else:
                chain_manager.latest_height = self.find_height_by_timestamp(chain_index, target_time)

        bootstrap_logger.info("BIFROST's Relayer: {}".format(RELAYER_VERSION))
        bootstrap_logger.info("Relayer-has-been-launched ({})".format(self.active_account.address.hex()))

        # run relayer
        self.run_eventbridge()

    @property
    def relayer_index(self) -> int:
        return self.cache.get_value(self.current_rnd)

    def is_validator(self, target_chain: ChainIndex, addr: EthAddress, is_initial: bool = True) -> bool:
        return self.world_call(target_chain, "relayer_authority", "is_selected_relayer", [addr.hex(), is_initial])[0]

    def is_previous_validator(
            self,
            target_chain: ChainIndex,
            round_num: int,
            addr: EthAddress,
            is_initial: bool = True
    ) -> bool:
        return self.world_call(
            target_chain,
            "relayer_authority",
            "is_previous_selected_relayer",
            [round_num, addr.hex(), is_initial]
        )[0]

    def fetch_validator_round(self, target_chain_index: ChainIndex) -> int:
        return self.world_call(target_chain_index, "relayer_authority", "latest_round", [])[0]  # unzip

    def fetch_validator_round_info(self) -> (int, int, int):
        resp = self.world_call(ChainIndex.BIFROST, "authority", "round_info", [])
        current_rnd_idx, fir_session_idx, current_session_index = resp[:3]
        first_rnd_block, first_session_block, current_block, round_length, session_length = resp[3:]
        return current_block, current_rnd_idx, round_length

    def fetch_sorted_validator_list(self, target_chain_index: ChainIndex, is_initial: bool = True) -> list:
        validator_tuple = self.world_call(target_chain_index, "relayer_authority", "selected_relayers", [is_initial])[0]  # unzip
        validator_list = list(validator_tuple)
        validator_list_lower = [addr.lower() for addr in validator_list]
        return sorted(validator_list_lower)

    def fetch_sorted_previous_validator_list(self, target_chain_index: ChainIndex, rnd: int, is_initial: bool = True) -> list:
        validator_tuple = self.world_call(target_chain_index, "relayer_authority", "previous_selected_relayers", [rnd, is_initial])[0]  # unzip
        validator_list = list(validator_tuple)
        validator_list_lower = [addr.lower() for addr in validator_list]
        return sorted(validator_list_lower)

    def fetch_lowest_validator_round(self) -> int:
        bottom_round = 2 ** 256 - 1
        for chain_index in self.supported_chain_list:
            round_num = self.fetch_validator_round(chain_index)
            if bottom_round > round_num:
                bottom_round = round_num
        return bottom_round

    def fetch_validator_num(self, target_chain_index: ChainIndex, is_initial: bool = True) -> int:
        validator_tuple = self.fetch_sorted_validator_list(target_chain_index, is_initial)
        return len(validator_tuple)

    def fetch_quorum(self, target_chain_index: ChainIndex, rnd: int = None, is_initial: bool = True) -> int:
        if rnd is None:
            majority = self.world_call(target_chain_index, "relayer_authority", "majority", [is_initial])[0]
        else:
            current_rnd = self.fetch_validator_round(target_chain_index)
            if current_rnd - rnd > 6:
                majority = 0
            else:
                majority = self.world_call(target_chain_index, "relayer_authority", "previous_majority", [rnd, is_initial])[0]
        return majority

    def fetch_socket_rbc_sigs(self, target_chain: ChainIndex, request_id: tuple):
        sigs = self.world_call(target_chain, "socket", "get_signatures", [request_id])
        return sigs[0]

    def fetch_socket_vsp_sigs(self, target_chain: ChainIndex, rnd: int):
        result = self.world_call(target_chain, "socket", "get_round_signatures", [rnd])
        return result[0]

    def fetch_oracle_latest_round(self, oracle_id: ConsensusOracleId):
        oracle_id_bytes = oracle_id.formatted_bytes()
        return self.world_call(ChainIndex.BIFROST, "oracle", "get_latest_round", [oracle_id_bytes])[0]

    def fetch_price_from_oracle(self, token: TokenStreamIndex) -> EthAmount:
        oid = AggOracleId.from_token_name(token.token_name())
        result = self.world_call(
            ChainIndex.BIFROST, "oracle", "latest_oracle_data", [oid.formatted_bytes()])[0]

        if token == TokenStreamIndex.USDT_ETHEREUM or token == TokenStreamIndex.USDC_ETHEREUM:
            decimal = 6
        else:
            decimal = 18

        return EthAmount(result, decimal)

    def fetch_btc_hash_from_oracle(self) -> EthHashBytes:
        oid = ConsensusOracleId.BTC_HASH
        result = self.world_call(
            ChainIndex.BIFROST, "oracle", "latest_oracle_data", [oid.formatted_bytes()])[0]
        return EthHashBytes(result)

    def is_pulsed_hear_beat(self) -> bool:
        relayer_addr = self.active_account.address
        return self.world_call(ChainIndex.BIFROST, "relayer_authority", "is_heartbeat_pulsed", [relayer_addr.hex()])[0]
