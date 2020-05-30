import json

import binascii
import base64
import websocket
import requests
from time import time
import random

import syft as sy
from syft.serde import protobuf

from syft.execution.state import State
from syft_proto.execution.v1.plan_pb2 import Plan as PlanPB
from syft_proto.execution.v1.state_pb2 import State as StatePB
from syft_proto.execution.v1.protocol_pb2 import Protocol as ProtocolPB

TIMEOUT_INTERVAL = 60
CHUNK_SIZE = 8192
SPEED_MULT_FACTOR = 10
MAX_BUFFER_SIZE = 1048576  # 1 MB
CHECK_SPEED_ITER = 10


class GridError(BaseException):
    def __init__(self, error, status):
        self.status = status
        self.error = error


class GridClient:
    CYCLE_STATUS_ACCEPTED = "accepted"
    CYCLE_STATUS_REJECTED = "rejected"
    PLAN_TYPE_LIST = "list"
    PLAN_TYPE_TORCHSCRIPT = "torchscript"

    def __init__(self, id: str, address: str, secure: bool = False):
        self.id = id
        self.address = address
        self.secure = secure
        self.ws = None
        self.serialize_worker = sy.VirtualWorker(hook=None)

    @property
    def ws_url(self):
        return f"wss://{self.address}" if self.secure else f"ws://{self.address}"

    @property
    def http_url(self):
        return f"https://{self.address}" if self.secure else f"http://{self.address}"

    def connect(self):
        args_ = {"max_size": None, "timeout": TIMEOUT_INTERVAL, "url": self.ws_url}

        self.ws = websocket.create_connection(**args_)

    def _send_msg(self, message: dict) -> dict:
        """ Prepare/send a JSON message to a PyGrid server and receive the response.

        Args:
            message (dict) : message payload.
        Returns:
            response (dict) : response payload.
        """
        if self.ws is None or not self.ws.connected:
            self.connect()

        self.ws.send(json.dumps(message))
        json_response = json.loads(self.ws.recv())

        # print("REQ", message)
        # print("RES", json_response)

        error = json_response["data"].get("error", None)
        if error is not None:
            raise GridError(error, None)

        return json_response

    def _send_http_req(self, method, path: str, params: dict = None, body: bytes = None):
        if method == "GET":
            res = requests.get(self.http_url + path, params)
        elif method == "POST":
            res = requests.post(self.http_url + path, params=params, data=body)

        if not res.ok:
            raise GridError("HTTP response is not OK", res.status_code)

        response = res.content
        return response

    def _yield_chunk_from_request(self, request, chunk_size):
        for chunk in request.iter_content(chunk_size=chunk_size):
            yield chunk

    def _read_n_request_chunks(self, chunk_generator, n):
        for i in range(n):
            try:
                next(chunk_generator)
            except:
                return False
        return True

    def _get_ping(self, worker_id, random_id):
        params = {
            "is_ping" : 1,
            "worker_id" : worker_id,
            "random" : random_id
        }
        start = time()
        self._send_http_req("GET", "/federated/speed-test", params)
        ping = (time() - start) * 1000  # milliseconds
        return ping

    def _get_upload_speed(self, worker_id, random_id):
        data_sample = b"x" * MAX_BUFFER_SIZE * 64  # 64 MB
        params = {
            "worker_id" : worker_id,
            "random" : random_id
        }
        body = {
            "upload_data" : data_sample
        }
        start = time()
        self._send_http_req("POST", "/federated/speed-test", params, body)
        upload_speed = 64 * 1024 / (time() - start())  # speed in KBps
        return upload_speed

    def _get_download_speed(self, worker_id, random_id):
        params = {
            "worker_id" : worker_id,
            "random" : random_id
        }
        speed_history = []
        prev_timestamp = time()
        with requests.get(self.http_url + path, params, stream=True) as r:
            r.raise_for_status()
            chunk_generator = self._yield_chunk_from_request(r, CHUNK_SIZE)
            while self._read_n_request_chunks(chunk_generator, buffer_size // CHUNK_SIZE):
                time_taken = time() - prev_timestamp
                if time_taken < 0.5:
                    buffer_size = min(buffer_size * SPEED_MULT_FACTOR, MAX_BUFFER_SIZE)
                    continue
                new_speed = buffer_size / (time_taken * 1024)
                speed_history.append(new_speed)
                if len(speed_history) % CHECK_SPEED_ITER == 0:
                    avg = sum(speed_history) / len(speed_history)
                    deviation = avg - min(speed_history)
                    if (deviation < 20) and (avg > 0):
                        break
                prev_timestamp = time()

        avg_speed = sum(speed_history) / len(speed_history)
        return avg_speed

    def _serialize(self, obj):
        """Serializes object to protobuf"""
        pb = protobuf.serde._bufferize(self.serialize_worker, obj)
        return pb.SerializeToString()

    def _serialize_object(self, obj):
        serialized_object = {}
        for k, v in obj.items():
            serialized_object[k] = binascii.hexlify(self._serialize(v)).decode()
        return serialized_object

    def _unserialize(self, serialized_obj, obj_protobuf_type):
        pb = obj_protobuf_type()
        pb.ParseFromString(serialized_obj)
        serialization_worker = sy.VirtualWorker(hook=None, auto_add=False)
        return protobuf.serde._unbufferize(serialization_worker, pb)

    def close(self):
        self.ws.shutdown()

    def host_federated_training(
        self,
        model,
        client_plans,
        client_protocols,
        client_config,
        server_averaging_plan,
        server_config,
    ):
        serialized_model = binascii.hexlify(self._serialize(model)).decode()
        serialized_plans = self._serialize_object(client_plans)
        serialized_protocols = self._serialize_object(client_protocols)
        serialized_avg_plan = binascii.hexlify(self._serialize(server_averaging_plan)).decode()

        # "federated/host-training" request body
        message = {
            "type": "federated/host-training",
            "data": {
                "model": serialized_model,
                "plans": serialized_plans,
                "protocols": serialized_protocols,
                "averaging_plan": serialized_avg_plan,
                "client_config": client_config,
                "server_config": server_config,
            },
        }

        return self._send_msg(message)

    def authenticate(self, auth_token):
        message = {
            "type": "federated/authenticate",
            "data": {"auth_token": auth_token},
        }

        return self._send_msg(message)

    def cycle_request(self, worker_id, model_name, model_version, speed_info):
        message = {
            "type": "federated/cycle-request",
            "data": {
                "worker_id": worker_id,
                "model": model_name,
                "version": model_version,
                **speed_info,
            },
        }
        return self._send_msg(message)

    def get_model(self, worker_id, request_key, model_id):
        params = {
            "worker_id": worker_id,
            "request_key": request_key,
            "model_id": model_id,
        }
        serialized_model = self._send_http_req("GET", "/federated/get-model", params)
        return self._unserialize(serialized_model, StatePB)

    def get_plan(self, worker_id, request_key, plan_id, receive_operations_as):
        params = {
            "worker_id": worker_id,
            "request_key": request_key,
            "plan_id": plan_id,
            "receive_operations_as": receive_operations_as,
        }
        serialized_plan = self._send_http_req("GET", "/federated/get-plan", params)
        return self._unserialize(serialized_plan, PlanPB)

    def get_protocol(self, worker_id, request_key, protocol_id):
        params = {
            "worker_id": worker_id,
            "request_key": request_key,
            "plan_id": protocol_id,
        }
        serialized_protocol = self._send_http_req("GET", "/federated/get-protocol", params)
        return self._unserialize(serialized_protocol, ProtocolPB)

    def report(self, worker_id: str, request_key: str, diff: State):
        diff_serialized = self._serialize(diff)
        diff_base64 = base64.b64encode(diff_serialized).decode("ascii")
        params = {
            "type": "federated/report",
            "data": {"worker_id": worker_id, "request_key": request_key, "diff": diff_base64},
        }
        return self._send_msg(params)

    def get_connection_speed(self, worker_id):
        random = random.getrandbits(128)
        ping = self._get_ping(worker_id, random)
        upload_speed = self._get_upload_speed(worker_id, random)
        download_speed = self._get_download_speed(worker_id, random)
        return {"ping": ping, "download": download_speed, "upload": upload_speed}
