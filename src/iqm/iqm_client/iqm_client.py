# Copyright 2021-2023 IQM client developers
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
r"""
Client for connecting to the IQM quantum computer server interface.

The :class:`Circuit` class represents quantum circuits to be executed, consisting of
native quantum operations, each represented by an instance of the :class:`Instruction` class.
Different Instruction types are distinguished by their :attr:`~Instruction.name`.
Each Instruction type acts on a number of :attr:`~Instruction.qubits`, and expects certain
:attr:`~Instruction.args`.


Instructions
============

We currently support the following native instruction types:

================ =========== ====================================== ===========
name             # of qubits args                                   description
================ =========== ====================================== ===========
measure          >= 1        ``key: str``                           Measurement in the Z basis.
prx              1           ``angle_t: float``, ``phase_t: float`` Phased x-rotation gate.
cz               2                                                  Controlled-Z gate.
barrier          >= 1                                               Execution barrier.
move             2                                                  Moves 1 state between resonator and qubit.
================ =========== ====================================== ===========

For each Instruction you may also optionally specify :attr:`~Instruction.implementation`,
which contains the name of an implementation of the instruction to use.
Support for multiple implementations is currently experimental and in normal use the
field should be omitted, this selects the default implementation for the instruction.

.. note::

   The following instruction names are deprecated, but supported for backwards compatibility for now:

   * ``phased_rx`` ↦ ``prx``
   * ``measurement`` ↦ ``measure``

Measure
-------

Measurement in the computational (Z) basis. The measurement results are the output of the circuit.
Takes one string argument, ``key``, denoting the measurement key the results are labeled with.
All the measurement keys in a circuit must be unique.
Each qubit may only be measured once.
The measurement must be the last operation on each qubit, i.e. it cannot be followed by gates.

.. code-block:: python
   :caption: Example

   Instruction(name='measure', qubits=('alice', 'bob', 'charlie'), args={'key': 'm1'})


PRX
---

Phased x-rotation gate, i.e. an x-rotation conjugated by a z-rotation.
Takes two arguments, the rotation angle ``angle_t`` and the phase angle ``phase_t``,
both measured in units of full turns (:math:`2\pi` radians).
The gate is represented in the standard computational basis by the matrix

.. math::
    \text{PRX}(\theta, \phi) = \exp(-i (X \cos (2 \pi \; \phi) + Y \sin (2 \pi \; \phi)) \: \pi \; \theta)
    = \text{RZ}(\phi) \: \text{RX}(\theta) \: \text{RZ}^\dagger(\phi),

where :math:`\theta` = ``angle_t``, :math:`\phi` = ``phase_t``,
and :math:`X` and :math:`Y` are Pauli matrices.

.. code-block:: python
   :caption: Example

   Instruction(name='prx', qubits=('bob',), args={'angle_t': 0.7, 'phase_t': 0.25})


CZ
--

Controlled-Z gate. Represented in the standard computational basis by the matrix

.. math:: \text{CZ} = \text{diag}(1, 1, 1, -1).

It is symmetric wrt. the qubits it's acting on, and takes no arguments.

.. code-block:: python
   :caption: Example

   Instruction(name='cz', qubits=('alice', 'bob'), args={})


MOVE
----

The MOVE operation is a unitary population exchange operation between a qubit and a resonator.
Its effect is only defined in the invariant subspace :math:`S = \text{span}\{|00\rangle, |01\rangle, |10\rangle\}`,
where it swaps the populations of the states :math:`|01\rangle` and :math:`|10\rangle`.
Its effect on the orthogonal subspace is undefined.

MOVE has the following presentation in the subspace :math:`S`:

.. math:: \text{MOVE}_S = |00\rangle \langle 00| + a |10\rangle \langle 01| + a^{-1} |01\rangle \langle 10|,

where :math:`a` is an undefined complex phase that is canceled when the MOVE gate is applied a second time.

To ensure that the state of the qubit and resonator has no overlap with :math:`|11\rangle`, it is
recommended that no single qubit gates are applied to the qubit in between a
pair of MOVE operations.

.. code-block:: python
   :caption: Example

   Instruction(name='move', qubits=('alice', 'resonator'), args={})


Barrier
-------

A barrier instruction affects the physical execution order of the instructions elsewhere in the
circuit that act on qubits spanned by the barrier.
It ensures that any such instructions that succeed the barrier are only executed after
all such instructions that precede the barrier have been completed.
Hence it can be used to guarantee a specific causal order for the other instructions.
It takes no arguments, and has no other effect.

.. code-block:: python
   :caption: Example

   Instruction(name='barrier', qubits=('alice', 'bob'), args={})

*Note*
1-qubit barriers will not have any effect on circuit's compilation and execution. Higher layers that sit on top of
IQM Client can make actual use of 1-qubit barriers (e.g. during circuit optimization), therefore having them is allowed.

Circuit output
==============

The :class:`RunResult` class represents the results of the quantum circuit execution job.
If the job succeeded, :attr:`RunResult.measurements` contains the output of the batch of circuits,
consisting of the results of the measurement operations in each circuit.
It is a list of dictionaries, where each dict maps each measurement key to a 2D array of measurement
results, represented as a nested list.
``RunResult.measurements[circuit_index][key][shot][qubit_index]`` is the result of measuring the
``qubit_index``'th qubit in measurement operation ``key`` in the shot ``shot`` in the
``circuit_index``'th circuit of the batch.

The results are non-negative integers representing the computational basis state (for qubits, 0 or 1)
that was the measurement outcome.

----
"""

# pylint: disable=too-many-lines

from __future__ import annotations

from base64 import b64decode
from datetime import datetime
from enum import Enum
from importlib.metadata import version
import json
import os
import platform
from posixpath import join
import time
from typing import Any, Callable, Optional, Union
from uuid import UUID
import warnings

from pydantic import BaseModel, Field, field_validator
import requests

from iqm.iqm_client.instruction import SUPPORTED_INSTRUCTIONS, Instruction
from iqm.iqm_client.quantum_architecture import QuantumArchitecture, QuantumArchitectureSpecification

REQUESTS_TIMEOUT = float(os.environ.get('IQM_CLIENT_REQUESTS_TIMEOUT', 60.0))

DEFAULT_TIMEOUT_SECONDS = 900
SECONDS_BETWEEN_CALLS = float(os.environ.get('IQM_CLIENT_SECONDS_BETWEEN_CALLS', 1.0))
REFRESH_MARGIN_SECONDS = 60

AUTH_CLIENT_ID = 'iqm_client'
AUTH_REALM = 'cortex'


class ClientConfigurationError(RuntimeError):
    """Wrong configuration provided."""


class ClientAuthenticationError(RuntimeError):
    """Something went wrong with user authentication."""


class CircuitValidationError(RuntimeError):
    """Circuit validation failed."""


class CircuitExecutionError(RuntimeError):
    """Something went wrong on the server."""


class APITimeoutError(CircuitExecutionError):
    """Exception for when executing a job on the server takes too long."""


class JobAbortionError(RuntimeError):
    """Job abortion failed."""


class Status(str, Enum):
    """
    Status of a job.
    """

    PENDING_COMPILATION = 'pending compilation'
    PENDING_EXECUTION = 'pending execution'
    READY = 'ready'
    FAILED = 'failed'
    ABORTED = 'aborted'
    PENDING_DELETION = 'pending deletion'
    DELETION_FAILED = 'deletion failed'
    DELETED = 'deleted'


class Circuit(BaseModel):
    """Quantum circuit to be executed."""

    name: str = Field(..., examples=['test circuit'])
    """name of the circuit"""
    instructions: tuple[Instruction, ...] = Field(...)
    """instructions comprising the circuit"""
    metadata: Optional[dict[str, Any]] = Field(None)
    """arbitrary metadata associated with the circuit"""

    def all_qubits(self) -> set[str]:
        """Return the names of all qubits in the circuit."""
        qubits: set[str] = set()
        for instruction in self.instructions:
            qubits.update(instruction.qubits)
        return qubits

    @field_validator('name')
    @classmethod
    def name_validator(cls, value):
        """Check if the circuit name is a non-empty string"""
        name = value
        if len(name) == 0:
            raise ValueError('A circuit should have a non-empty string for a name.')
        return name

    @field_validator('instructions')
    @classmethod
    def instructions_validator(cls, value):
        """Check the container of instructions and each instruction within"""
        instructions = value

        # Check container type
        if not isinstance(instructions, (list, tuple)):
            raise ValueError('Instructions of a circuit should be packed in a tuple')

        # Check if any instructions are present
        if len(value) == 0:
            raise ValueError('Each circuit should have at least one instruction.')

        # Check each instruction explicitly, because automatic validation for Instruction
        # is only called when we create a new instance of Instruction, but not if we modify
        # an existing instance.
        for instruction in instructions:
            if isinstance(instruction, Instruction):
                Instruction.model_validate(instruction.__dict__)
            else:
                raise ValueError('Every instruction in a circuit should be of type <Instruction>')

        return instructions


CircuitBatch = list[Circuit]
"""Type that represents a list of quantum circuits to be executed together in a single batch."""


class SingleQubitMapping(BaseModel):
    """Mapping of a logical qubit name to a physical qubit name."""

    logical_name: str = Field(..., examples=['alice'])
    """logical qubit name"""
    physical_name: str = Field(..., examples=['QB1'])
    """physical qubit name"""


QubitMapping = list[SingleQubitMapping]
"""Type that represents a qubit mapping for a circuit, i.e. a list of single qubit mappings
for all qubits in the circuit."""


class HeraldingMode(str, Enum):
    """Heralding mode for circuit execution.

    Heralding is the practice of generating data about the state of qubits prior to execution of a circuit.
    This can be achieved by measuring the qubits immediately before executing each shot for a circuit."""

    NONE = 'none'
    """Do not do any heralding."""
    ZEROS = 'zeros'
    """Perform a heralding measurement, only retain shots with an all-zeros result.

    Note: in this mode, the number of shots returned after execution will be less or equal to the requested amount
    due to the post-selection based on heralding data."""


class RunRequest(BaseModel):
    """Request for an IQM quantum computer to run a job that executes a batch of quantum circuits.

    Note: all circuits in a batch must measure the same qubits otherwise batch execution fails.
    """

    circuits: CircuitBatch = Field(...)
    """batch of quantum circuit(s) to execute"""
    custom_settings: Optional[dict[str, Any]] = Field(None)
    """Custom settings to override default IQM hardware settings and calibration data.
Note: This field should be always None in normal use."""
    calibration_set_id: Optional[UUID] = Field(None)
    """ID of the calibration set to use, or None to use the latest calibration set"""
    qubit_mapping: Optional[list[SingleQubitMapping]] = Field(None)
    """mapping of logical qubit names to physical qubit names, or None if using physical qubit names"""
    shots: int = Field(..., gt=0)
    """how many times to execute each circuit in the batch, must be greater than zero"""
    max_circuit_duration_over_t2: Optional[float] = Field(None)
    """Circuits are disqualified on the server if they are longer than this ratio
        of the T2 time of the qubits.
        If set to 0.0, no circuits are disqualified. If set to None the server default value is used."""
    heralding_mode: HeraldingMode = Field(HeraldingMode.NONE)
    """which heralding mode to use during the execution of circuits in this request."""


CircuitMeasurementResults = dict[str, list[list[int]]]
"""Measurement results from a single circuit. For each measurement operation in the circuit,
maps the measurement key to the corresponding results. The outer list elements correspond to shots,
and the inner list elements to the qubits measured in the measurement operation."""


CircuitMeasurementResultsBatch = list[CircuitMeasurementResults]
"""Type that represents measurement results for a batch of circuits."""


class Metadata(BaseModel):
    """Metadata describing a circuit execution job."""

    calibration_set_id: Optional[UUID] = Field(None)
    """ID of the calibration set used"""
    request: RunRequest = Field(...)
    """copy of the original RunRequest sent to the server"""
    cocos_version: Optional[str] = Field(None)
    """CoCoS version used to execute the job"""
    timestamps: Optional[dict[str, str]] = Field(None)
    """Timestamps of execution progress"""


class RunResult(BaseModel):
    """Results of a circuit execution job.

    * ``measurements`` is present iff the status is ``'ready'``.
    * ``message`` carries additional information for the ``'failed'`` status.
    * If the status is ``'pending compilation'`` or ``'pending execution'``, ``measurements`` and ``message`` are
      ``None``.
    """

    status: Status = Field(...)
    """current status of the job, in ``{'pending compilation', 'pending execution', 'ready', 'failed', 'aborted'}``"""
    measurements: Optional[CircuitMeasurementResultsBatch] = Field(None)
    """if the job has finished successfully, the measurement results for the circuit(s)"""
    message: Optional[str] = Field(None)
    """if the job failed, an error message"""
    metadata: Metadata = Field(...)
    """metadata about the job"""
    warnings: Optional[list[str]] = Field(None)
    """list of warning messages"""

    @staticmethod
    def from_dict(inp: dict[str, Union[str, dict]]) -> 'RunResult':
        """Parses the result from a dict.

        Args:
            inp: value to parse, has to map to RunResult

        Returns:
            parsed job result

        """
        input_copy = inp.copy()
        return RunResult(status=Status(input_copy.pop('status')), **input_copy)


class RunStatus(BaseModel):
    """Status of a circuit execution job."""

    status: Status = Field(...)
    """current status of the job, in ``{'pending compilation', 'pending execution', 'ready', 'failed', 'aborted'}``"""
    message: Optional[str] = Field(None)
    """if the job failed, an error message"""
    warnings: Optional[list[str]] = Field(None)
    """list of warning messages"""

    @staticmethod
    def from_dict(inp: dict[str, Union[str, dict]]) -> 'RunStatus':
        """Parses the result from a dict.

        Args:
            inp: value to parse, has to map to RunResult

        Returns:
            parsed job status

        """
        input_copy = inp.copy()
        return RunStatus(status=Status(input_copy.pop('status')), **input_copy)


class GrantType(str, Enum):
    """
    Type of token request.
    """

    PASSWORD = 'password'
    REFRESH = 'refresh_token'


class AuthRequest(BaseModel):
    """Request sent to authentication server for access token and refresh token, or for terminating the session.

    * Token request with grant type ``'password'`` starts a new session in the authentication server.
      It uses fields ``client_id``, ``grant_type``, ``username`` and ``password``.
    * Token request with grant type ``'refresh_token'`` is used for maintaining an existing session.
      It uses field ``client_id``, ``grant_type``, ``refresh_token``.
    * Logout request uses only fields ``client_id`` and ``refresh_token``.

    """

    client_id: str = Field(...)
    """name of the client for all request types"""
    grant_type: Optional[GrantType] = Field(None)
    """type of token request, in ``{'password', 'refresh_token'}``"""
    username: Optional[str] = Field(None)
    """username for grant type ``'password'``"""
    password: Optional[str] = Field(None)
    """password for grant type ``'password'``"""
    refresh_token: Optional[str] = Field(None)
    """refresh token for grant type ``'refresh_token'`` and logout request"""


class Credentials(BaseModel):
    """Credentials and tokens for maintaining a session with the authentication server.

    * Fields ``auth_server_url``, ``username`` and ``password`` are provided by the user.
    * Fields ``access_token`` and ``refresh_token`` are loaded from the authentication server and
      refreshed periodically.
    """

    auth_server_url: str = Field(...)
    """Base URL of the authentication server"""
    username: str = Field(...)
    """username for logging in to the server"""
    password: str = Field(...)
    """password for logging in to the server"""
    access_token: Optional[str] = Field(None)
    """current access token of the session"""
    refresh_token: Optional[str] = Field(None)
    """current refresh token of the session"""


class ExternalToken(BaseModel):
    """Externally managed token for maintaining a session with the authentication server.

    * Fields ``auth_server_url`` and ``access_token`` are loaded from an
      external resource, e.g. file generated by Cortex CLI's token manager.
    """

    auth_server_url: str = Field(...)
    """Base URL of the authentication server"""
    access_token: str = Field(...)
    """current access token of the session"""


def update_batch_circuit_metadata(circuit_metadata, circuits):
    """Iterates over circuits in batch and extends circuit metadata dictionary with
    data from circuit_metadata

    Args:
        circuit_metadata: Key-value pairs representing additional circuit metadata
        circuits: list of circuits for which metadata should be updated

    Returns:
        new list of circuits with updated metadata
    """

    for k, v in circuit_metadata.items():
        for circuit in circuits:
            circuit.metadata[k] = v

    return circuits


def serialize_qubit_mapping(qubit_mapping: dict[str, str]) -> list[SingleQubitMapping]:
    """Serializes a qubit mapping dict into the corresponding IQM data transfer format.

    Args:
        qubit_mapping: mapping from logical to physical qubit names

    Returns:
        data transfer object representing the mapping
    """
    return [SingleQubitMapping(logical_name=k, physical_name=v) for k, v in qubit_mapping.items()]


def validate_circuit(circuit: Circuit) -> None:
    """Validates a submitted quantum circuit using Pydantic tooling. If the
    validation of the circuit fails, an exception is raised.

    Args:
        circuit: a circuit that needs validation

    Returns:
         None

    Raises:
            pydantic.error_wrappers.ValidationError
    """
    Circuit.model_validate(circuit.__dict__)


def _get_credentials(credentials: dict[str, str]) -> Optional[Credentials]:
    """Try to obtain credentials, first from arguments, then from environment variables.

    Args:
        credentials: dict of credentials provided as arguments

    Returns:
        Credentials with token fields cleared, or None if ``auth_server_url`` was not set.
    """
    auth_server_url = credentials.get('auth_server_url') or os.environ.get('IQM_AUTH_SERVER')
    username = credentials.get('username') or os.environ.get('IQM_AUTH_USERNAME')
    password = credentials.get('password') or os.environ.get('IQM_AUTH_PASSWORD')
    if not auth_server_url:
        return None
    if not username or not password:
        raise ClientConfigurationError('Auth server URL is set but no username or password')
    return Credentials(auth_server_url=auth_server_url, username=username, password=password)


def _get_external_token(tokens_file: Optional[str] = None) -> Optional[ExternalToken]:
    """Try to obtain external token from a file, first by path provided, then by path from
    environment variable.

    Args:
        tokens_file: path to a JSON file containing tokens

    Returns:
        ExternalToken with non-empty auth_server_url and access_token fields,
        or None if ``tokens_file`` was not provided.
    """

    filepath = tokens_file or os.environ.get('IQM_TOKENS_FILE')

    if not filepath:
        return None

    try:
        with open(filepath, 'r', encoding='utf-8') as file:
            raw_data = file.read()
    except FileNotFoundError as error:
        raise ClientConfigurationError(f'File not found: {filepath}') from error

    try:
        json_data = json.loads(raw_data)
    except json.decoder.JSONDecodeError as error:
        raise ClientConfigurationError(f'Decoding JSON has failed, {error}') from error

    auth_server_url = json_data['auth_server_url']
    access_token = json_data['access_token']

    return ExternalToken(auth_server_url=auth_server_url, access_token=access_token)


def _time_left_seconds(token: str) -> int:
    """Check how much time is left until the token expires.

    Returns:
        Time left on token in seconds.
    """
    _, body, _ = token.split('.', 2)
    # Add padding to adjust body length to a multiple of 4 chars as required by base64 decoding
    body += '=' * (-len(body) % 4)
    exp_time = int(json.loads(b64decode(body)).get('exp', '0'))
    return max(0, exp_time - int(time.time()))


class IQMClient:
    """Provides access to IQM quantum computers.

    Args:
        url: Endpoint for accessing the server. Has to start with http or https.
        client_signature: String that IQMClient adds to User-Agent header of requests
            it sends to the server. The signature is appended to IQMClients own version
            information and is intended to carry additional version information,
            for example the version information of the caller.
        tokens_file: Optional path to a tokens file used for authentication.
            This can also be set in the IQM_TOKENS_FILE environment variable.
            If tokens_file is set, auth_server_url, username and password
            must not be set.
        token: If an IQM token is long-lived and the server provides it in plain text format,
            it can be passed in this argument. It can also be provided in the IQM_TOKEN
            environment variable. If ``tokens_file`` (or IQM_TOKENS_FILE) is set, it will
            override the value of this token.

    Keyword Args:
        auth_server_url (str): Optional base URL of the authentication server.
            This can also be set in the IQM_AUTH_SERVER environment variable.
            If unset, requests will be sent unauthenticated.
        username (str): Optional username to log in to authentication server.
            This can also be set in the IQM_AUTH_USERNAME environment variable.
            Username must be set if ``auth_server_url`` is set.
        password (str): Optional password to log in to authentication server.
            This can also be set in the IQM_AUTH_PASSWORD environment variable.
            Password must be set if ``auth_server_url`` is set.
    """

    # pylint: disable=too-many-instance-attributes
    def __init__(
        self,
        url: str,
        *,
        client_signature: Optional[str] = None,
        tokens_file: Optional[str] = None,
        token: Optional[str] = None,
        **credentials,  # contains auth_server_url, username, password
    ):
        if not url.startswith(('http:', 'https:')):
            raise ClientConfigurationError(f'The URL schema has to be http or https. Incorrect schema in URL: {url}')
        if tokens_file and credentials:
            raise ClientConfigurationError('Either external token or credentials must be provided. Both were provided.')
        self._base_url = url
        self._signature = f'{platform.platform(terse=True)}'
        self._signature += f', python {platform.python_version()}'
        self._signature += f', iqm-client {version("iqm-client")}'
        if client_signature:
            self._signature += f', {client_signature}'
        self._tokens_file = tokens_file
        self._external_token = _get_external_token(tokens_file)
        self._token = token or os.environ.get('IQM_TOKEN')
        self._architecture: QuantumArchitectureSpecification | None = None
        if not self._external_token:
            self._credentials = _get_credentials(credentials)
            self._update_tokens()
        self._project_id = os.environ.get('PROJECT_ID', None)
        self._slurm_job_id = os.environ.get('SLURM_JOB_ID', None)

    def __del__(self):
        try:
            # try our best to close the auth session, doesn't matter if it fails,
            # refresh token will be re-issued for the same credentials or eventually expire
            if not self._external_token:
                self.close_auth_session()
        except Exception:  # pylint: disable=broad-except
            pass

    def _retry_request_on_error(self, request: Callable[[], requests.Response]) -> requests.Response:
        """This is a temporary workaround for 502 errors.
        The current implementation of the server side can run out of network connections
        and silently drop incoming connections making IQM Client to fail with 502 errors."""

        while True:
            result = request()
            if result.status_code == 502:
                time.sleep(SECONDS_BETWEEN_CALLS)
                continue
            break

        return result

    # pylint: disable=too-many-locals
    def submit_circuits(
        self,
        circuits: CircuitBatch,
        *,
        qubit_mapping: Optional[dict[str, str]] = None,
        custom_settings: Optional[dict[str, Any]] = None,
        calibration_set_id: Optional[UUID] = None,
        shots: int = 1,
        max_circuit_duration_over_t2: Optional[float] = None,
        heralding_mode: HeraldingMode = HeraldingMode.NONE,
    ) -> UUID:
        """Submits a batch of quantum circuits for execution on a quantum computer.

        Args:
            circuits: list of circuits to be executed
            qubit_mapping: Mapping of logical qubit names to physical qubit names.
                Can be set to ``None`` if all ``circuits`` already use physical qubit names.
                Note that the ``qubit_mapping`` is used for all ``circuits``.
            custom_settings: Custom settings to override default settings and calibration data.
                Note: This field should always be ``None`` in normal use.
            calibration_set_id: ID of the calibration set to use, or ``None`` to use the latest one
            shots: number of times ``circuits`` are executed, value must be greater than zero
            max_circuit_duration_over_t2: Circuits are disqualified on the server if they are longer than this ratio
                of the T2 time of the qubits. Setting this value to ``0.0`` turns off circuit duration checking.
                The default value ``None`` instructs server to use server's default value in the checking.
            heralding_mode: Heralding mode to use during the execution.

        Returns:
            ID for the created job. This ID is needed to query the job status and the execution results.
        """

        if shots < 1:
            raise ValueError('Number of shots must be greater than zero.')

        for i, circuit in enumerate(circuits):
            try:
                validate_circuit(circuit)
            except ValueError as e:
                raise CircuitValidationError(f'The circuit at index {i} failed the validation').with_traceback(
                    e.__traceback__
                )

        architecture = self.get_quantum_architecture()

        self._validate_qubit_mapping(architecture, circuits, qubit_mapping)
        serialized_qubit_mapping = serialize_qubit_mapping(qubit_mapping) if qubit_mapping else None

        self._validate_circuit_instructions(architecture, circuits, qubit_mapping)

        # Metadata to attach to circuits
        additional_metadata = {
            'project_id': self._project_id,
            'slurm_job_id': self._slurm_job_id
        }
        # Filter the metadata
        additional_metadata = {k: v for k,v in additional_metadata.items()  if v is not None}
        # Attach metadata to circuits metadata
        circuits = update_batch_circuit_metadata(additional_metadata, circuits)
        
        # ``bearer_token`` can be ``None`` if cocos we're connecting does not use authentication
        bearer_token = self._get_bearer_token()

        data = RunRequest(
            qubit_mapping=serialized_qubit_mapping,
            circuits=circuits,
            custom_settings=custom_settings,
            calibration_set_id=calibration_set_id,
            shots=shots,
            max_circuit_duration_over_t2=max_circuit_duration_over_t2,
            heralding_mode=heralding_mode,
        )

        headers = {'Expect': '100-Continue', 'User-Agent': self._signature}
        if bearer_token:
            headers['Authorization'] = bearer_token

        try:
            # check if someone is trying to profile us with OpenTelemetry
            # pylint: disable=import-outside-toplevel
            # pylint: disable=import-error
            from opentelemetry import propagate

            propagate.inject(headers)
        except ImportError as _:
            # no OpenTelemetry, no problem
            pass

        result = self._retry_request_on_error(
            lambda: requests.post(
                join(self._base_url, 'jobs'),
                json=json.loads(data.model_dump_json(exclude_none=True)),
                headers=headers,
                timeout=REQUESTS_TIMEOUT,
            )
        )

        if result.status_code == 401:
            raise ClientAuthenticationError(f'Authentication failed: {result.text}')

        if 400 <= result.status_code < 500:
            raise ClientConfigurationError(f'Client configuration error: {result.text}')

        result.raise_for_status()

        try:
            job_id = UUID(result.json()['id'])
            return job_id
        except (json.decoder.JSONDecodeError, KeyError) as e:
            raise CircuitExecutionError(f'Invalid response: {result.text}, {e}') from e

    @staticmethod
    def _validate_qubit_mapping(
        architecture: QuantumArchitectureSpecification,
        circuits: CircuitBatch,
        qubit_mapping: Optional[dict[str, str]] = None,
    ):
        """Validates the given qubit mapping, if defined.

        Args:
          architecture: the quantum architecture to check against
          circuits: list of circuits to be checked
          qubit_mapping: Mapping of logical qubit names to physical qubit names.
              Can be set to ``None`` if all ``circuits`` already use physical qubit names.
              Note that the ``qubit_mapping`` is used for all ``circuits``.

        Raises:
            CircuitExecutionError: IQM server specific exceptions
        """
        if qubit_mapping is None:
            return

        # check if qubit mapping is injective
        target_qubits = set(qubit_mapping.values())
        if not len(target_qubits) == len(qubit_mapping):
            raise ValueError('Multiple logical qubits map to the same physical qubit.')

        # check if qubit mapping covers all qubits in the circuits
        for i, circuit in enumerate(circuits):
            diff = circuit.all_qubits() - set(qubit_mapping)
            if diff:
                raise ValueError(
                    f"The qubits {diff} in circuit '{circuit.name}' at index {i} "
                    f'are not found in the provided qubit mapping.'
                )

        # check that each mapped qubit is defined in the quantum architecture
        for _logical, physical in qubit_mapping.items():
            if physical not in architecture.qubits:
                raise CircuitExecutionError(f'Qubit {physical} not present in quantum architecture')

    @staticmethod
    def _validate_circuit_instructions(
        architecture: QuantumArchitectureSpecification,
        circuits: CircuitBatch,
        qubit_mapping: Optional[dict[str, str]] = None,
    ):
        """Validates that the instructions target correct qubits in the given circuits.

        Args:
          architecture: the quantum architecture to check against
          circuits: list of circuits to be checked
          qubit_mapping: Mapping of logical qubit names to physical qubit names.
              Can be set to ``None`` if all ``circuits`` already use physical qubit names.
              Note that the ``qubit_mapping`` is used for all ``circuits``.

        Raises:
            CircuitExecutionError: IQM server specific exceptions
        """
        for circuit in circuits:
            for instr in circuit.instructions:
                IQMClient._validate_instruction(architecture, instr, qubit_mapping)

    @staticmethod
    def _validate_instruction(
        architecture: QuantumArchitectureSpecification,
        instruction: Instruction,
        qubit_mapping: Optional[dict[str, str]] = None,
    ):
        """Validates that the instruction targets correct qubits in the given architecture.

        Args:
          architecture: the quantum architecture to check against
          instruction: the instruction to check
          qubit_mapping: Mapping of logical qubit names to physical qubit names.
              Can be set to ``None`` if all ``circuits`` already use physical qubit names.
              Note that the ``qubit_mapping`` is used for all ``circuits``.

        Raises:
            CircuitExecutionError: IQM server specific exceptions
        """
        if instruction.name not in architecture.operations:
            raise ValueError(f"Instruction '{instruction.name}' is not supported by the quantum architecture.")
        allowed_loci = architecture.operations[instruction.name]
        qubits = [qubit_mapping[q] for q in instruction.qubits] if qubit_mapping else list(instruction.qubits)
        info = SUPPORTED_INSTRUCTIONS[instruction.name]
        check_locus = info['check_locus'] if 'check_locus' in info else None
        if check_locus is False:
            # Should skip locus check (e.g. for barrier)
            return
        if check_locus == 'any_combination':
            # Check that all qubits in the locus are allowed by the architecture
            allowed_qubits = set(q for locus in allowed_loci for q in locus)
            for q in instruction.qubits:
                mapped_q = qubit_mapping[q] if qubit_mapping else q
                if mapped_q not in allowed_qubits:
                    raise CircuitExecutionError(
                        f'Qubit {q} = {mapped_q} is not allowed as locus for {instruction.name}'
                        if qubit_mapping
                        else f'Qubit {q} is not allowed as locus for {instruction.name}'
                    )
            return

        # Check that locus matches one of the loci defined in architecture
        is_directed = 'directed' in info and info['directed'] is True
        all_loci = allowed_loci if is_directed else [qs for pair in allowed_loci for qs in [pair, pair[::-1]]]
        if qubits not in all_loci:
            raise CircuitExecutionError(
                f'{instruction.qubits} = {tuple(qubits)} not allowed as locus for {instruction.name}'
                if qubit_mapping
                else f'{instruction.qubits} not allowed as locus for {instruction.name}'
            )

    def get_run(self, job_id: UUID, *, timeout_secs: float = REQUESTS_TIMEOUT) -> RunResult:
        """Query the status and results of a submitted job.

        Args:
            job_id: id of the job to query
            timeout_secs: network request timeout

        Returns:
            result of the job (can be pending)

        Raises:
            CircuitExecutionError: IQM server specific exceptions
            HTTPException: HTTP exceptions
        """
        result = self._retry_request_on_error(
            lambda: requests.get(
                join(self._base_url, 'jobs', str(job_id)),
                headers=self._default_headers(),
                timeout=timeout_secs,
            )
        )

        result.raise_for_status()
        try:
            run_result = RunResult.from_dict(result.json())
        except (json.decoder.JSONDecodeError, KeyError) as e:
            raise CircuitExecutionError(f'Invalid response: {result.text}, {e}') from e

        if run_result.warnings:
            for warning in run_result.warnings:
                warnings.warn(warning)
        if run_result.status == Status.FAILED:
            raise CircuitExecutionError(run_result.message)
        return run_result

    def get_run_status(self, job_id: UUID, *, timeout_secs: float = REQUESTS_TIMEOUT) -> RunStatus:
        """Query the status of a submitted job.

        Args:
            job_id: id of the job to query
            timeout_secs: network request timeout

        Returns:
            status of the job

        Raises:
            CircuitExecutionError: IQM server specific exceptions
            HTTPException: HTTP exceptions
        """
        result = self._retry_request_on_error(
            lambda: requests.get(
                join(self._base_url, 'jobs', str(job_id), 'status'),
                headers=self._default_headers(),
                timeout=timeout_secs,
            )
        )

        result.raise_for_status()
        try:
            run_result = RunStatus.from_dict(result.json())
        except (json.decoder.JSONDecodeError, KeyError) as e:
            raise CircuitExecutionError(f'Invalid response: {result.text}, {e}') from e

        if run_result.warnings:
            for warning in run_result.warnings:
                warnings.warn(warning)
        return run_result

    def wait_for_compilation(self, job_id: UUID, timeout_secs: float = DEFAULT_TIMEOUT_SECONDS) -> RunResult:
        """Poll results until a job is either pending execution, ready, failed, aborted, or timed out.

        Args:
            job_id: id of the job to wait for
            timeout_secs: how long to wait for a response before raising an APITimeoutError

        Returns:
            job result

        Raises:
            APITimeoutError: time exceeded the set timeout
        """
        start_time = datetime.now()
        while (datetime.now() - start_time).total_seconds() < timeout_secs:
            status = self.get_run_status(job_id).status
            if status != Status.PENDING_COMPILATION:
                return self.get_run(job_id)
            time.sleep(SECONDS_BETWEEN_CALLS)
        raise APITimeoutError(f"The job compilation didn't finish in {timeout_secs} seconds.")

    def wait_for_results(self, job_id: UUID, timeout_secs: float = DEFAULT_TIMEOUT_SECONDS) -> RunResult:
        """Poll results until a job is either ready, failed, aborted, or timed out.
           Note, that jobs handling on the server side is async and if we try to request the results
           right after submitting the job (which is usually the case)
           we will find the job is still pending at least for the first query.

        Args:
            job_id: id of the job to wait for
            timeout_secs: how long to wait for a response before raising an APITimeoutError

        Returns:
            job result

        Raises:
            APITimeoutError: time exceeded the set timeout
        """
        start_time = datetime.now()
        while (datetime.now() - start_time).total_seconds() < timeout_secs:
            status = self.get_run_status(job_id).status
            if status not in (Status.PENDING_COMPILATION, Status.PENDING_EXECUTION):
                return self.get_run(job_id)
            time.sleep(SECONDS_BETWEEN_CALLS)
        raise APITimeoutError(f"The job didn't finish in {timeout_secs} seconds.")

    def abort_job(self, job_id: UUID, *, timeout_secs: float = REQUESTS_TIMEOUT) -> None:
        """Abort a job that was submitted for execution.

        Args:
            job_id: id of the job to be aborted
            timeout_secs: network request timeout

        Raises:
            HTTPException: HTTP exceptions
            JobAbortionError: if aborting the job failed
        """
        result = requests.post(
            join(self._base_url, 'jobs', str(job_id), 'abort'),
            headers=self._default_headers(),
            timeout=timeout_secs,
        )
        if result.status_code != 200:
            raise JobAbortionError(result.text)

    def get_quantum_architecture(self, *, timeout_secs: float = REQUESTS_TIMEOUT) -> QuantumArchitectureSpecification:
        """Retrieve quantum architecture from server.
        Caches the result and returns the same result on later invocations.

        Args:
            timeout_secs: network request timeout

        Returns:
            quantum architecture

        Raises:
            APITimeoutError: time exceeded the set timeout
            ClientConfigurationError: if no valid authentication is provided
            HTTPException: HTTP exceptions
        """
        if self._architecture:
            return self._architecture

        result = requests.get(
            join(self._base_url, 'quantum-architecture'),
            headers=self._default_headers(),
            timeout=timeout_secs,
        )

        # /quantum_architecture is not a strictly authenticated endpoint,
        # so we need to handle 302 redirects to the auth server login page
        if result.history and any(
            response.status_code == 302 for response in result.history
        ):  # pragma: no cover (generators are broken in coverage)
            raise ClientConfigurationError('Authentication is required.')
        if result.status_code == 401:
            raise ClientAuthenticationError(f'Authentication failed: {result.text}')

        result.raise_for_status()
        try:
            qa = QuantumArchitecture(**result.json()).quantum_architecture
        except (json.decoder.JSONDecodeError, KeyError) as e:
            raise CircuitExecutionError(f'Invalid response: {result.text}, {e}') from e
        # Cache architecture so that later invocations do not need to query it again
        self._architecture = qa
        return qa

    def close_auth_session(self) -> bool:
        """Terminate session with authentication server if there was one created.

        Returns:
            True iff session was successfully closed

        Raises:
            ClientAuthenticationError: if logout failed
            ClientAuthenticationError: if asked to close externally managed authentication session
        """
        # auth session is managed externally, unable to close it here
        if self._external_token:
            raise ClientAuthenticationError('Unable to close externally managed auth session')

        # no auth, nothing to close
        if self._credentials is None:
            return False

        # auth session wasn't started, nothing to close
        if not self._credentials.refresh_token:
            return False

        url = f'{self._credentials.auth_server_url}/realms/{AUTH_REALM}/protocol/openid-connect/logout'
        data = AuthRequest(client_id=AUTH_CLIENT_ID, refresh_token=self._credentials.refresh_token)
        result = requests.post(url, data=data.model_dump(exclude_none=True), timeout=REQUESTS_TIMEOUT)
        if result.status_code not in [200, 204]:
            raise ClientAuthenticationError(f'Logout failed, {result.text}')
        self._credentials.access_token = None
        self._credentials.refresh_token = None
        return True

    def _get_bearer_token(self, retries: int = 1) -> Optional[str]:  # pylint: disable=too-many-return-statements
        """Make a bearer token for Authorization header. If token is about to expire, refresh it first.

        Args:
            retries: number of times to try updating the tokens

        Returns:
            Bearer token, i.e. string containing prefix 'Bearer ' and the access token, or None if access token
            is not available.
        """
        if self._external_token:
            # If access token obtained from external tokens file expires soon, get updated token from the tokens file
            if _time_left_seconds(self._external_token.access_token) < REFRESH_MARGIN_SECONDS:
                self._external_token = _get_external_token(self._tokens_file)
                if not self._external_token:
                    return None
            return f'Bearer {self._external_token.access_token}'
        if self._token:
            return f'Bearer {self._token}'
        if self._credentials is None or not self._credentials.access_token:
            return None
        if _time_left_seconds(self._credentials.access_token) > REFRESH_MARGIN_SECONDS:
            # Access token is still valid, no need to refresh
            return f'Bearer {self._credentials.access_token}'
        if retries < 1:
            return None
        self._update_tokens()
        return self._get_bearer_token(retries - 1)

    def _update_tokens(self):
        """Update access token and refresh token.

        Uses refresh token to request new tokens from authentication server.  If the refresh token has expired or
        is about to expire, starts a new session by requesting the tokens using username and password.

        Updated tokens are stored in ``self._credentials``.

        Raises:
            ClientAuthenticationError: updating the tokens failed
        """
        if self._credentials is None:
            return

        refresh_token = self._credentials.refresh_token
        if refresh_token and _time_left_seconds(refresh_token) > REFRESH_MARGIN_SECONDS:
            # Update tokens using existing refresh_token
            data = AuthRequest(client_id=AUTH_CLIENT_ID, grant_type=GrantType.REFRESH, refresh_token=refresh_token)
        else:
            # Update tokens using username and password
            data = AuthRequest(
                client_id=AUTH_CLIENT_ID,
                grant_type=GrantType.PASSWORD,
                username=self._credentials.username,
                password=self._credentials.password,
            )

        url = f'{self._credentials.auth_server_url}/realms/{AUTH_REALM}/protocol/openid-connect/token'
        result = requests.post(url, data=data.model_dump(exclude_none=True), timeout=REQUESTS_TIMEOUT)
        if result.status_code != 200:
            raise ClientAuthenticationError(f'Failed to update tokens, {result.text}')
        tokens = result.json()
        self._credentials.access_token = tokens.get('access_token')
        self._credentials.refresh_token = tokens.get('refresh_token')

    def _default_headers(self):
        headers = {'User-Agent': self._signature}
        bearer_token = self._get_bearer_token()
        if bearer_token:
            headers['Authorization'] = bearer_token
        return headers
