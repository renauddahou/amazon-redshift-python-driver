import logging
import os
import socket
import typing
from collections import deque
from copy import deepcopy
from datetime import datetime as Datetime
from datetime import timedelta as Timedelta
from decimal import Decimal
from distutils.version import LooseVersion
from hashlib import md5
from itertools import count
from os import getpid
from struct import pack
from typing import TYPE_CHECKING
from warnings import warn

from scramp import ScramClient  # type: ignore

from redshift_connector.config import (
    DEFAULT_PROTOCOL_VERSION,
    ClientProtocolVersion,
    _client_encoding,
    max_int2,
    max_int4,
    max_int8,
    min_int2,
    min_int4,
    min_int8,
    pg_array_types,
    pg_to_py_encodings,
)
from redshift_connector.cursor import Cursor
from redshift_connector.error import (
    ArrayContentNotHomogenousError,
    ArrayContentNotSupportedError,
    DatabaseError,
    Error,
    IntegrityError,
    InterfaceError,
    InternalError,
    NotSupportedError,
    OperationalError,
    ProgrammingError,
    Warning,
)
from redshift_connector.utils import (
    FC_BINARY,
    FC_TEXT,
    NULL,
    NULL_BYTE,
    DriverInfo,
    array_check_dimensions,
    array_dim_lengths,
    array_find_first_element,
    array_flatten,
    array_has_null,
    array_recv_binary,
    array_recv_text,
    bh_unpack,
    cccc_unpack,
    ci_unpack,
    date_in,
    date_recv_binary,
    float_array_recv,
    geographyhex_recv,
    h_pack,
    h_unpack,
    i_pack,
    i_unpack,
    ihihih_unpack,
    ii_pack,
    iii_pack,
    int_array_recv,
    make_divider_block,
    numeric_in,
    numeric_in_binary,
)
from redshift_connector.utils import pg_types as PG_TYPES
from redshift_connector.utils import py_types as PY_TYPES
from redshift_connector.utils import (
    q_pack,
    text_recv,
    time_in,
    time_recv_binary,
    timetz_in,
    timetz_recv_binary,
    varbytehex_recv,
    walk_array,
)
from redshift_connector.utils.type_utils import (
    BIGINT,
    DATE,
    GEOGRAPHY,
    INTEGER,
    INTEGER_ARRAY,
    NUMERIC,
    REAL_ARRAY,
    SMALLINT,
    SMALLINT_ARRAY,
    TEXT_ARRAY,
    TIME,
    TIMESTAMP,
    TIMESTAMPTZ,
    TIMETZ,
    VARBYTE,
    VARCHAR_ARRAY,
)

if TYPE_CHECKING:
    from ssl import SSLSocket

# Copyright (c) 2007-2009, Mathieu Fenniak
# Copyright (c) The Contributors
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met:
#
# * Redistributions of source code must retain the above copyright notice,
# this list of conditions and the following disclaimer.
# * Redistributions in binary form must reproduce the above copyright notice,
# this list of conditions and the following disclaimer in the documentation
# and/or other materials provided with the distribution.
# * The name of the author may not be used to endorse or promote products
# derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

__author__ = "Mathieu Fenniak"

_logger: logging.Logger = logging.getLogger(__name__)

ZERO: Timedelta = Timedelta(0)
BINARY: type = bytes


# The purpose of this function is to change the placeholder of original query into $1, $2
# in order to be identified by database
# example: INSERT INTO book (title) VALUES (:title) -> INSERT INTO book (title) VALUES ($1)
# also return the function: make_args()
def convert_paramstyle(style: str, query) -> typing.Tuple[str, typing.Any]:
    # I don't see any way to avoid scanning the query string char by char,
    # so we might as well take that careful approach and create a
    # state-based scanner.  We'll use int variables for the state.
    OUTSIDE: int = 0  # outside quoted string
    INSIDE_SQ: int = 1  # inside single-quote string '...'
    INSIDE_QI: int = 2  # inside quoted identifier   "..."
    INSIDE_ES: int = 3  # inside escaped single-quote string, E'...'
    INSIDE_PN: int = 4  # inside parameter name eg. :name
    INSIDE_CO: int = 5  # inside inline comment eg. --

    in_quote_escape: bool = False
    in_param_escape: bool = False
    placeholders: typing.List[str] = []
    output_query: typing.List[str] = []
    param_idx: typing.Iterator[str] = map(lambda x: "$" + str(x), count(1))
    state: int = OUTSIDE
    prev_c: typing.Optional[str] = None
    for i, c in enumerate(query):
        if i + 1 < len(query):
            next_c = query[i + 1]
        else:
            next_c = None

        if state == OUTSIDE:
            if c == "'":
                output_query.append(c)
                if prev_c == "E":
                    state = INSIDE_ES
                else:
                    state = INSIDE_SQ
            elif c == '"':
                output_query.append(c)
                state = INSIDE_QI
            elif c == "-":
                output_query.append(c)
                if prev_c == "-":
                    state = INSIDE_CO
            elif style == "qmark" and c == "?":
                output_query.append(next(param_idx))
            elif style == "numeric" and c == ":" and next_c not in ":=" and prev_c != ":":
                # Treat : as beginning of parameter name if and only
                # if it's the only : around
                # Needed to properly process type conversions
                # i.e. sum(x)::float
                output_query.append("$")
            elif style == "named" and c == ":" and next_c not in ":=" and prev_c != ":":
                # Same logic for : as in numeric parameters
                state = INSIDE_PN
                placeholders.append("")
            elif style == "pyformat" and c == "%" and next_c == "(":
                state = INSIDE_PN
                placeholders.append("")
            elif style in ("format", "pyformat") and c == "%":
                style = "format"
                if in_param_escape:
                    in_param_escape = False
                    output_query.append(c)
                else:
                    if next_c == "%":
                        in_param_escape = True
                    elif next_c == "s":
                        state = INSIDE_PN
                        output_query.append(next(param_idx))
                    else:
                        raise InterfaceError("Only %s and %% are supported in the query.")
            else:
                output_query.append(c)

        elif state == INSIDE_SQ:
            if c == "'":
                if in_quote_escape:
                    in_quote_escape = False
                else:
                    if next_c == "'":
                        in_quote_escape = True
                    else:
                        state = OUTSIDE
            output_query.append(c)

        elif state == INSIDE_QI:
            if c == '"':
                state = OUTSIDE
            output_query.append(c)

        elif state == INSIDE_ES:
            if c == "'" and prev_c != "\\":
                # check for escaped single-quote
                state = OUTSIDE
            output_query.append(c)

        elif state == INSIDE_PN:
            if style == "named":
                placeholders[-1] += c
                if next_c is None or (not next_c.isalnum() and next_c != "_"):
                    state = OUTSIDE
                    try:
                        pidx: int = placeholders.index(placeholders[-1], 0, -1)
                        output_query.append("$" + str(pidx + 1))
                        del placeholders[-1]
                    except ValueError:
                        output_query.append("$" + str(len(placeholders)))
            elif style == "pyformat":
                if prev_c == ")" and c == "s":
                    state = OUTSIDE
                    try:
                        pidx = placeholders.index(placeholders[-1], 0, -1)
                        output_query.append("$" + str(pidx + 1))
                        del placeholders[-1]
                    except ValueError:
                        output_query.append("$" + str(len(placeholders)))
                elif c in "()":
                    pass
                else:
                    placeholders[-1] += c
            elif style == "format":
                state = OUTSIDE

        elif state == INSIDE_CO:
            output_query.append(c)
            if c == "\n":
                state = OUTSIDE

        prev_c = c

    if style in ("numeric", "qmark", "format"):

        def make_args(vals):
            return vals

    else:

        def make_args(vals):
            return tuple(vals[p] for p in placeholders)

    return "".join(output_query), make_args


# Message codes
# ALl communication is through a stream of messages
# Driver will send one or more messages to database,
# and database will respond one or more messages
# The first byte of a message specify the type of the message
NOTICE_RESPONSE: bytes = b"N"
AUTHENTICATION_REQUEST: bytes = b"R"
PARAMETER_STATUS: bytes = b"S"
BACKEND_KEY_DATA: bytes = b"K"
READY_FOR_QUERY: bytes = b"Z"
ROW_DESCRIPTION: bytes = b"T"
ERROR_RESPONSE: bytes = b"E"
DATA_ROW: bytes = b"D"
COMMAND_COMPLETE: bytes = b"C"
PARSE_COMPLETE: bytes = b"1"
BIND_COMPLETE: bytes = b"2"
CLOSE_COMPLETE: bytes = b"3"
PORTAL_SUSPENDED: bytes = b"s"
NO_DATA: bytes = b"n"
PARAMETER_DESCRIPTION: bytes = b"t"
NOTIFICATION_RESPONSE: bytes = b"A"
COPY_DONE: bytes = b"c"
COPY_DATA: bytes = b"d"
COPY_IN_RESPONSE: bytes = b"G"
COPY_OUT_RESPONSE: bytes = b"H"
EMPTY_QUERY_RESPONSE: bytes = b"I"

BIND: bytes = b"B"
PARSE: bytes = b"P"
EXECUTE: bytes = b"E"
FLUSH: bytes = b"H"
SYNC: bytes = b"S"
PASSWORD: bytes = b"p"
DESCRIBE: bytes = b"D"
TERMINATE: bytes = b"X"
CLOSE: bytes = b"C"


# This inform the format of a message
# the first byte, the code, will be the type of the message
# then add the 4 bytes to inform the length of rest of message
# then add the real data we want to send
def create_message(code: bytes, data: bytes = b"") -> bytes:
    return code + typing.cast(bytes, i_pack(len(data) + 4)) + data


FLUSH_MSG: bytes = create_message(FLUSH)
SYNC_MSG: bytes = create_message(SYNC)
TERMINATE_MSG: bytes = create_message(TERMINATE)
COPY_DONE_MSG: bytes = create_message(COPY_DONE)
EXECUTE_MSG: bytes = create_message(EXECUTE, NULL_BYTE + i_pack(0))

# DESCRIBE constants
STATEMENT: bytes = b"S"
PORTAL: bytes = b"P"

# ErrorResponse codes
RESPONSE_SEVERITY: str = "S"  # always present
RESPONSE_SEVERITY = "V"  # always present
RESPONSE_CODE: str = "C"  # always present
RESPONSE_MSG: str = "M"  # always present
RESPONSE_DETAIL: str = "D"
RESPONSE_HINT: str = "H"
RESPONSE_POSITION: str = "P"
RESPONSE__POSITION: str = "p"
RESPONSE__QUERY: str = "q"
RESPONSE_WHERE: str = "W"
RESPONSE_FILE: str = "F"
RESPONSE_LINE: str = "L"
RESPONSE_ROUTINE: str = "R"

IDLE: bytes = b"I"
IDLE_IN_TRANSACTION: bytes = b"T"
IDLE_IN_FAILED_TRANSACTION: bytes = b"E"

arr_trans: typing.Mapping[int, typing.Optional[str]] = dict(zip(map(ord, "[] 'u"), ["{", "}", None, None, None]))


class Connection:
    # DBAPI Extension: supply exceptions as attributes on the connection
    Warning = property(lambda self: self._getError(Warning))
    Error = property(lambda self: self._getError(Error))
    InterfaceError = property(lambda self: self._getError(InterfaceError))
    DatabaseError = property(lambda self: self._getError(DatabaseError))
    OperationalError = property(lambda self: self._getError(OperationalError))
    IntegrityError = property(lambda self: self._getError(IntegrityError))
    InternalError = property(lambda self: self._getError(InternalError))
    ProgrammingError = property(lambda self: self._getError(ProgrammingError))
    NotSupportedError = property(lambda self: self._getError(NotSupportedError))

    def __enter__(self: "Connection") -> "Connection":
        return self

    def __exit__(self: "Connection", exc_type, exc_value, traceback) -> None:
        self.close()

    def _getError(self: "Connection", error):
        warn("DB-API extension connection.%s used" % error.__name__, stacklevel=3)
        return error

    @property
    def client_os_version(self: "Connection") -> str:
        from platform import platform as CLIENT_PLATFORM

        try:
            os_version: str = CLIENT_PLATFORM()
        except:
            os_version = "unknown"
        return os_version

    def __init__(
        self: "Connection",
        user: str,
        password: str,
        database: str,
        host: str = "localhost",
        port: int = 5439,
        source_address: typing.Optional[str] = None,
        unix_sock: typing.Optional[str] = None,
        ssl: bool = True,
        sslmode: str = "verify-ca",
        timeout: typing.Optional[int] = None,
        max_prepared_statements: int = 1000,
        tcp_keepalive: typing.Optional[bool] = True,
        application_name: typing.Optional[str] = None,
        replication: typing.Optional[str] = None,
        client_protocol_version: int = DEFAULT_PROTOCOL_VERSION,
        database_metadata_current_db_only: bool = True,
        credentials_provider: typing.Optional[str] = None,
        provider_name: typing.Optional[str] = None,
        web_identity_token: typing.Optional[str] = None,
    ):
        """
        Creates a :class:`Connection` to an Amazon Redshift cluster. For more information on establishing a connection to an Amazon Redshift cluster using `federated API access <https://aws.amazon.com/blogs/big-data/federated-api-access-to-amazon-redshift-using-an-amazon-redshift-connector-for-python/>`_ see our examples page.
        This is the underlying :class:`Connection` constructor called from :func:`redshift_connector.connect`.

        Parameters
        ----------
        user : str
            The username to use for authentication with the Amazon Redshift cluster.
        password : str
            The password to use for authentication with the Amazon Redshift cluster.
        database : str
            The name of the database instance to connect to.
        host : str
            The hostname of the Amazon Redshift cluster.
        port : int
            The port number of the Amazon Redshift cluster. Default value is 5439.
        source_address : Optional[str]
        unix_sock : Optional[str]
        ssl : bool
            Is SSL enabled. Default value is ``True``. SSL must be enabled when authenticating using IAM.
        sslmode : str
            The security of the connection to the Amazon Redshift cluster. 'verify-ca' and 'verify-full' are supported.
        timeout : Optional[int]
            The number of seconds before the connection to the server will timeout. By default there is no timeout.
        max_prepared_statements : int
        tcp_keepalive : Optional[bool]
            Is `TCP keepalive <https://en.wikipedia.org/wiki/Keepalive#TCP_keepalive>`_ used. The default value is ``True``.
        application_name : Optional[str]
            Sets the application name. The default value is None.
        replication : Optional[str]
            Used to run in `streaming replication mode <https://www.postgresql.org/docs/12/protocol-replication.html>`_.
        client_protocol_version : int
            The requested server protocol version. The default value is 1 representing `EXTENDED_RESULT_METADATA`. If the requested server protocol cannot be satisfied, a warning will be displayed to the user.
        database_metadata_current_db_only : bool
            Is `datashare <https://docs.aws.amazon.com/redshift/latest/dg/datashare-overview.html>`_ disabled. Default value is True, implying datasharing will not be used.
        credentials_provider : Optional[str]
            The class-path of the IdP plugin used for authentication with Amazon Redshift.
        provider_name : Optional[str]
            The name of the Redshift Native Auth Provider.
        web_identity_token: Optional[str]
            A web identity token used for authentication via Redshift Native IDP Integration
        """
        self.merge_socket_read = True

        _client_encoding = "utf8"
        self._commands_with_count: typing.Tuple[bytes, ...] = (
            b"INSERT",
            b"DELETE",
            b"UPDATE",
            b"MOVE",
            b"FETCH",
            b"COPY",
            b"SELECT",
        )
        self.notifications: deque = deque(maxlen=100)
        self.notices: deque = deque(maxlen=100)
        self.parameter_statuses: deque = deque(maxlen=100)
        self.max_prepared_statements: int = int(max_prepared_statements)
        self._run_cursor: Cursor = Cursor(self, paramstyle="named")
        self._client_protocol_version: int = client_protocol_version
        self._database = database
        self.py_types = deepcopy(PY_TYPES)
        self.pg_types = deepcopy(PG_TYPES)
        self._database_metadata_current_db_only: bool = database_metadata_current_db_only

        # based on _client_protocol_version value, we must use different conversion functions
        # for receiving some datatypes
        self._enable_protocol_based_conversion_funcs()

        self.web_identity_token = web_identity_token

        if user is None:
            raise InterfaceError("The 'user' connection parameter cannot be None")

        redshift_native_auth: bool = False

        init_params: typing.Dict[str, typing.Optional[typing.Union[str, bytes]]] = {
            "user": "",
            "database": database,
            "application_name": application_name,
            "replication": replication,
            "client_protocol_version": str(self._client_protocol_version),
            "driver_version": DriverInfo.driver_full_name(),
            "os_version": self.client_os_version,
        }

        if credentials_provider:
            init_params["plugin_name"] = credentials_provider

            if credentials_provider.split(".")[-1] in (
                "BasicJwtCredentialsProvider",
                "BrowserAzureOAuth2CredentialsProvider",
            ):
                redshift_native_auth = True
                init_params["idp_type"] = "AzureAD"

                if provider_name:
                    init_params["provider_name"] = provider_name

        if not redshift_native_auth or user:
            init_params["user"] = user

        _logger.debug(make_divider_block())
        _logger.debug("Establishing a connection")
        _logger.debug(init_params)
        _logger.debug(make_divider_block())

        for k, v in tuple(init_params.items()):
            if isinstance(v, str):
                init_params[k] = v.encode("utf8")
            elif v is None:
                del init_params[k]
            elif not isinstance(v, (bytes, bytearray)):
                raise InterfaceError("The parameter " + k + " can't be of type " + str(type(v)) + ".")

        if "user" in init_params:
            self.user: bytes = typing.cast(bytes, init_params["user"])
        else:
            self.user = b""

        if isinstance(password, str):
            self.password: bytes = password.encode("utf8")
        else:
            self.password = password

        self.autocommit: bool = False
        self._xid = None

        self._caches: typing.Dict = {}

        # Create the TCP/Ip socket and connect to specific database
        # if there already has a socket, it will not create new connection when run connect again
        try:
            if unix_sock is None and host is not None:
                self._usock: typing.Union[socket.socket, "SSLSocket"] = socket.socket(
                    socket.AF_INET, socket.SOCK_STREAM
                )
                if source_address is not None:
                    self._usock.bind((source_address, 0))
            elif unix_sock is not None:
                if not hasattr(socket, "AF_UNIX"):
                    raise InterfaceError("attempt to connect to unix socket on unsupported " "platform")
                self._usock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            else:
                raise ProgrammingError("one of host or unix_sock must be provided")
            if timeout is not None:
                self._usock.settimeout(timeout)

            if unix_sock is None and host is not None:
                self._usock.connect((host, port))
            elif unix_sock is not None:
                self._usock.connect(unix_sock)

            # For Redshift, we the default ssl approve is True
            # create ssl connection with Redshift CA certificates and check the hostname
            if ssl is True:
                try:
                    from ssl import CERT_REQUIRED, SSLContext

                    # ssl_context = ssl.create_default_context()

                    path = os.path.abspath(__file__)
                    if os.name == "nt":
                        path = "\\".join(path.split("\\")[:-1]) + "\\files\\redshift-ca-bundle.crt"
                    else:
                        path = "/".join(path.split("/")[:-1]) + "/files/redshift-ca-bundle.crt"

                    ssl_context: SSLContext = SSLContext()
                    ssl_context.verify_mode = CERT_REQUIRED
                    ssl_context.load_default_certs()
                    ssl_context.load_verify_locations(path)

                    # Int32(8) - Message length, including self.
                    # Int32(80877103) - The SSL request code.
                    self._usock.sendall(ii_pack(8, 80877103))
                    resp: bytes = self._usock.recv(1)
                    if resp != b"S":
                        _logger.debug(
                            "Server response code when attempting to establish ssl connection: {!r}".format(resp)
                        )
                        raise InterfaceError("Server refuses SSL")

                    if sslmode == "verify-ca":
                        self._usock = ssl_context.wrap_socket(self._usock)
                    elif sslmode == "verify-full":
                        ssl_context.check_hostname = True
                        self._usock = ssl_context.wrap_socket(self._usock, server_hostname=host)

                except ImportError:
                    raise InterfaceError("SSL required but ssl module not available in " "this python installation")

            self._sock: typing.Optional[typing.BinaryIO] = self._usock.makefile(mode="rwb")
            if tcp_keepalive:
                self._usock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except socket.error as e:
            self._usock.close()
            raise InterfaceError("communication error", e)
        self._flush: typing.Callable = self._sock.flush
        self._read: typing.Callable = self._sock.read
        self._write: typing.Callable = self._sock.write
        self._backend_key_data: typing.Optional[bytes] = None

        trans_tab = dict(zip(map(ord, "{}"), "[]"))
        glbls = {"Decimal": Decimal}

        self.inspect_funcs: typing.Dict[type, typing.Callable] = {
            Datetime: self.inspect_datetime,
            list: self.array_inspect,
            tuple: self.array_inspect,
            int: self.inspect_int,
        }

        # it's a dictionary whose key is type of message,
        # value is the corresponding function to process message
        self.message_types: typing.Dict[bytes, typing.Callable] = {
            NOTICE_RESPONSE: self.handle_NOTICE_RESPONSE,
            AUTHENTICATION_REQUEST: self.handle_AUTHENTICATION_REQUEST,
            PARAMETER_STATUS: self.handle_PARAMETER_STATUS,
            BACKEND_KEY_DATA: self.handle_BACKEND_KEY_DATA,
            READY_FOR_QUERY: self.handle_READY_FOR_QUERY,
            ROW_DESCRIPTION: self.handle_ROW_DESCRIPTION,
            ERROR_RESPONSE: self.handle_ERROR_RESPONSE,
            EMPTY_QUERY_RESPONSE: self.handle_EMPTY_QUERY_RESPONSE,
            DATA_ROW: self.handle_DATA_ROW,
            COMMAND_COMPLETE: self.handle_COMMAND_COMPLETE,
            PARSE_COMPLETE: self.handle_PARSE_COMPLETE,
            BIND_COMPLETE: self.handle_BIND_COMPLETE,
            CLOSE_COMPLETE: self.handle_CLOSE_COMPLETE,
            PORTAL_SUSPENDED: self.handle_PORTAL_SUSPENDED,
            NO_DATA: self.handle_NO_DATA,
            PARAMETER_DESCRIPTION: self.handle_PARAMETER_DESCRIPTION,
            NOTIFICATION_RESPONSE: self.handle_NOTIFICATION_RESPONSE,
            COPY_DONE: self.handle_COPY_DONE,
            COPY_DATA: self.handle_COPY_DATA,
            COPY_IN_RESPONSE: self.handle_COPY_IN_RESPONSE,
            COPY_OUT_RESPONSE: self.handle_COPY_OUT_RESPONSE,
        }

        # Int32 - Message length, including self.
        # Int32(196608) - Protocol version number.  Version 3.0.
        # Any number of key/value pairs, terminated by a zero byte:
        #   String - A parameter name (user, database, or options)
        #   String - Parameter value

        # Conduct start-up communication with database
        # Message's first part is the protocol version - Int32(196608)
        protocol: int = 196608
        val: bytearray = bytearray(i_pack(protocol))

        # Message include parameters name and value (user, database, application_name, replication)
        for k, v in init_params.items():
            val.extend(k.encode("ascii") + NULL_BYTE + typing.cast(bytes, v) + NULL_BYTE)
        val.append(0)
        # Use write and flush function to write the content of the buffer
        # and then send the message to the database
        self._write(i_pack(len(val) + 4))
        self._write(val)
        self._flush()

        self._cursor: Cursor = self.cursor()

        code = None
        self.error: typing.Optional[Exception] = None
        _logger.debug("Sending start-up message")
        # When driver send the start-up message to database, DB will respond multi messages to driver
        # whose format is same with the message that driver send to DB.
        while code not in (READY_FOR_QUERY, ERROR_RESPONSE):
            # Thus use a loop to process each message
            # Each time will read 5 bytes, the first byte, the code, inform the type of message
            # following 4 bytes inform the message's length
            # then can use this length to minus 4 to get the real data.
            code, data_len = ci_unpack(self._read(5))
            self.message_types[code](self._read(data_len - 4), None)
        if self.error is not None:
            raise self.error

        # if we didn't receive a server_protocol_version from the server, default to
        # using BASE_SERVER as the server is likely lacking this functionality due to
        # being out of date
        if (
            self._client_protocol_version > ClientProtocolVersion.BASE_SERVER
            and not (b"server_protocol_version", str(self._client_protocol_version).encode()) in self.parameter_statuses
        ):
            _logger.debug("Server_protocol_version not received from server")
            self._client_protocol_version = ClientProtocolVersion.BASE_SERVER
            self._enable_protocol_based_conversion_funcs()

        self.in_transaction = False

    def _enable_protocol_based_conversion_funcs(self: "Connection"):
        if self._client_protocol_version >= ClientProtocolVersion.BINARY.value:
            self.pg_types[NUMERIC] = (FC_BINARY, numeric_in_binary)
            self.pg_types[DATE] = (FC_BINARY, date_recv_binary)
            self.pg_types[GEOGRAPHY] = (FC_BINARY, geographyhex_recv)  # GEOGRAPHY
            self.pg_types[TIME] = (FC_BINARY, time_recv_binary)
            self.pg_types[TIMETZ] = (FC_BINARY, timetz_recv_binary)
            self.pg_types[1002] = (FC_BINARY, array_recv_binary)  # CHAR[]
            self.pg_types[SMALLINT_ARRAY] = (FC_BINARY, array_recv_binary)  # INT2[]
            self.pg_types[INTEGER_ARRAY] = (FC_BINARY, array_recv_binary)  # INT4[]
            self.pg_types[TEXT_ARRAY] = (FC_BINARY, array_recv_binary)  # TEXT[]
            self.pg_types[VARCHAR_ARRAY] = (FC_BINARY, array_recv_binary)  # VARCHAR[]
            self.pg_types[REAL_ARRAY] = (FC_BINARY, array_recv_binary)  # FLOAT4[]
            self.pg_types[1028] = (FC_BINARY, array_recv_binary)  # OID[]
            self.pg_types[1034] = (FC_BINARY, array_recv_binary)  # ACLITEM[]
            self.pg_types[VARBYTE] = (FC_TEXT, text_recv)  # VARBYTE
        else:  # text protocol
            self.pg_types[NUMERIC] = (FC_TEXT, numeric_in)
            self.pg_types[TIME] = (FC_TEXT, time_in)
            self.pg_types[DATE] = (FC_TEXT, date_in)
            self.pg_types[GEOGRAPHY] = (FC_TEXT, text_recv)  # GEOGRAPHY
            self.pg_types[TIMETZ] = (FC_BINARY, timetz_recv_binary)
            self.pg_types[1002] = (FC_TEXT, array_recv_text)  # CHAR[]
            self.pg_types[SMALLINT_ARRAY] = (FC_TEXT, int_array_recv)  # INT2[]
            self.pg_types[INTEGER_ARRAY] = (FC_TEXT, int_array_recv)  # INT4[]
            self.pg_types[TEXT_ARRAY] = (FC_TEXT, array_recv_text)  # TEXT[]
            self.pg_types[VARCHAR_ARRAY] = (FC_TEXT, array_recv_text)  # VARCHAR[]
            self.pg_types[REAL_ARRAY] = (FC_TEXT, float_array_recv)  # FLOAT4[]
            self.pg_types[1028] = (FC_TEXT, int_array_recv)  # OID[]
            self.pg_types[1034] = (FC_TEXT, array_recv_text)  # ACLITEM[]
            self.pg_types[VARBYTE] = (FC_TEXT, varbytehex_recv)  # VARBYTE

    @property
    def _is_multi_databases_catalog_enable_in_server(self: "Connection") -> bool:
        if (b"datashare_enabled", str("on").encode()) in self.parameter_statuses:
            return True
        else:
            # if we don't receive this param from the server, we do not support
            return False

    @property
    def is_single_database_metadata(self):
        return self._database_metadata_current_db_only or not self._is_multi_databases_catalog_enable_in_server

    def handle_ERROR_RESPONSE(self: "Connection", data, ps):
        """
        Handler for ErrorResponse message received via Amazon Redshift wire protocol, represented by b'E' code.

        ErrorResponse (B)
            Byte1('E')
                Identifies the message as an error.

            Int32
                Length of message contents in bytes, including self.

                The message body consists of one or more identified fields, followed by a zero byte as a terminator. Fields may appear in any order. For each field there is the following:

            Byte1
            A code identifying the field type; if zero, this is the message terminator and no string follows. The presently defined field types are listed in Section 42.5. Since more field types may be added in future, frontends should silently ignore fields of unrecognized type.

            String
                The field value.

        Parameters
        ----------
        :param data: bytes:
            Message content
        :param ps: typing.Optional[typing.Dict[str, typing.Any]]:
            Prepared Statement from associated Cursor

        Returns
        -------
        None:None
        """
        msg: typing.Dict[str, str] = dict(
            (s[:1].decode(_client_encoding), s[1:].decode(_client_encoding)) for s in data.split(NULL_BYTE) if s != b""
        )

        response_code: str = msg[RESPONSE_CODE]
        if response_code == "28000":
            cls: type = InterfaceError
        elif response_code == "23505":
            cls = IntegrityError
        else:
            cls = ProgrammingError

        self.error = cls(msg)

    def handle_EMPTY_QUERY_RESPONSE(self: "Connection", data, ps):
        """
        Handler for EmptyQueryResponse message received via Amazon Redshift wire protocol, represented by b'I' code.

        EmptyQueryResponse (B)
            Byte1('I')
                Identifies the message as a response to an empty query string. (This substitutes for CommandComplete.)

            Int32(4)
                Length of message contents in bytes, including self.

        Parameters
        ----------
        :param data: bytes:
            Message content
        :param ps: typing.Optional[typing.Dict[str, typing.Any]]:
            Prepared Statement from associated Cursor

        Returns
        -------
        None:None
        """
        self.error = ProgrammingError("query was empty")

    def handle_CLOSE_COMPLETE(self: "Connection", data, ps):
        """
        Handler for CloseComplete message received via Amazon Redshift wire protocol, represented by b'3' code. Currently a
        no-op.

        CloseComplete (B)
            Byte1('3')
                Identifies the message as a Close-complete indicator.

            Int32(4)
                Length of message contents in bytes, including self.

        Parameters
        ----------
        :param data: bytes:
            Message content
        :param ps: typing.Optional[typing.Dict[str, typing.Any]]:
            Prepared Statement from associated Cursor

        Returns
        -------
        None:None
        """
        pass

    def handle_PARSE_COMPLETE(self: "Connection", data, ps):
        """
        Handler for ParseComplete message received via Amazon Redshift wire protocol, represented by b'1' code. Currently a
        no-op.

        ParseComplete (B)
            Byte1('1')
                Identifies the message as a Parse-complete indicator.

            Int32(4)
            Length of message contents in bytes, including self.

        Parameters
        ----------
        :param data: bytes:
            Message content
        :param ps: typing.Optional[typing.Dict[str, typing.Any]]:
            Prepared Statement from associated Cursor

        Returns
        -------
        None:None
        """
        pass

    def handle_BIND_COMPLETE(self: "Connection", data, ps):
        """
        Handler for BindComplete message received via Amazon Redshift wire protocol, represented by b'2' code. Currently a
        no-op.

        BindComplete (B)
            Byte1('2')
                Identifies the message as a Bind-complete indicator.

            Int32(4)
                Length of message contents in bytes, including self.

        Parameters
        ----------
        :param data: bytes:
            Message content
        :param ps: typing.Optional[typing.Dict[str, typing.Any]]:
            Prepared Statement from associated Cursor

        Returns
        -------
        None:None
        """
        pass

    def handle_PORTAL_SUSPENDED(self: "Connection", data, cursor: Cursor):
        """
        Handler for PortalSuspend message received via Amazon Redshift wire protocol, represented by b's' code. Currently a
        no-op.

        PortalSuspended (B)
            Byte1('s')
                Identifies the message as a portal-suspended indicator. Note this only appears if an Execute message's row-count limit was reached.

            Int32(4)
                Length of message contents in bytes, including self.

        Parameters
        ----------
        :param data: bytes:
            Message content
        :param cursor: `Cursor`
            The `Cursor` object associated with the given statements execution.

        Returns
        -------
        None:None
        """
        pass

    def handle_PARAMETER_DESCRIPTION(self: "Connection", data, ps):
        """
        Handler for ParameterDescription message received via Amazon Redshift wire protocol, represented by b't' code.

        ParameterDescription (B)
            Byte1('t')
                Identifies the message as a parameter description.

            Int32
                Length of message contents in bytes, including self.

            Int16
                The number of parameters used by the statement (may be zero).

                Then, for each parameter, there is the following:

            Int32
                Specifies the object ID of the parameter data type.

        Parameters
        ----------
        :param data: bytes:
            Message content
        :param ps: typing.Optional[typing.Dict[str, typing.Any]]:
            Prepared Statement from associated Cursor

        Returns
        -------
        None:None
        """
        # Well, we don't really care -- we're going to send whatever we
        # want and let the database deal with it.  But thanks anyways!

        # count = h_unpack(data)[0]
        # type_oids = unpack_from("!" + "i" * count, data, 2)
        pass

    def handle_COPY_DONE(self: "Connection", data, ps):
        """
        Handler for CopyDone message received via Amazon Redshift wire protocol, represented by b'c' code.

        CopyDone (F & B)
            Byte1('c')
                Identifies the message as a COPY-complete indicator.

            Int32(4)
                Length of message contents in bytes, including self.

        Parameters
        ----------
        :param data: bytes:
            Message content
        :param ps: typing.Optional[typing.Dict[str, typing.Any]]:
            Prepared Statement from associated Cursor

        Returns
        -------
        None:None
        """
        self._copy_done = True

    def handle_COPY_OUT_RESPONSE(self: "Connection", data, ps):
        """
        Handler for CopyOutResponse message received via Amazon Redshift wire protocol, represented by b'H' code.

        CopyOutResponse (B)
            Byte1('H')
                Identifies the message as a Start Copy Out response. This message will be followed by copy-out data.

            Int32
                Length of message contents in bytes, including self.

            Int8
                0 indicates the overall COPY format is textual (rows separated by newlines, columns separated by separator characters, etc). 1 indicates the overall copy format is binary (similar to DataRow format). See COPY for more information.

            Int16
                The number of columns in the data to be copied (denoted N below).

            Int16[N]
                The format codes to be used for each column. Each must presently be zero (text) or one (binary). All must be zero if the overall copy format is textual.

        Parameters
        ----------
        :param data: bytes:
            Message content
        :param ps: typing.Optional[typing.Dict[str, typing.Any]]:
            Prepared Statement from associated Cursor

        Returns
        -------
        None:None
        """
        is_binary, num_cols = bh_unpack(data)
        # column_formats = unpack_from('!' + 'h' * num_cols, data, 3)
        if ps.stream is None:
            raise InterfaceError("An output stream is required for the COPY OUT response.")

    def handle_COPY_DATA(self: "Connection", data, ps) -> None:
        """
        Handler for CopyData message received via Amazon Redshift wire protocol, represented by b'd' code.

        CopyData (F & B)
            Byte1('d')
                Identifies the message as COPY data.

            Int32
                Length of message contents in bytes, including self.

            Byten
                Data that forms part of a COPY data stream. Messages sent from the backend will always correspond to single data rows, but messages sent by frontends may divide the data stream arbitrarily.

        Parameters
        ----------
        :param data: bytes:
            Message content
        :param ps: typing.Optional[typing.Dict[str, typing.Any]]:
            Prepared Statement from associated Cursor

        Returns
        -------
        None:None
        """
        ps.stream.write(data)

    def handle_COPY_IN_RESPONSE(self: "Connection", data, ps):
        """
        Handler for CopyInResponse message received via Amazon Redshift wire protocol, represented by b'G' code.

        CopyInResponse (B)
            Byte1('G')
                Identifies the message as a Start Copy In response. The frontend must now send copy-in data (if not prepared to do so, send a CopyFail message).

            Int32
                Length of message contents in bytes, including self.

            Int8
                0 indicates the overall COPY format is textual (rows separated by newlines, columns separated by separator characters, etc). 1 indicates the overall copy format is binary (similar to DataRow format). See COPY for more information.

            Int16
                The number of columns in the data to be copied (denoted N below).

            Int16[N]
                The format codes to be used for each column. Each must presently be zero (text) or one (binary). All must be zero if the overall copy format is textual.

        Parameters
        ----------
        :param data: bytes:
            Message content
        :param ps: typing.Optional[typing.Dict[str, typing.Any]]:
            Prepared Statement from associated Cursor

        Returns
        -------
        None:None
        """
        # Int16(2) - Number of columns
        # Int16(N) - Format codes for each column (0 text, 1 binary)
        is_binary, num_cols = bh_unpack(data)
        # column_formats = unpack_from('!' + 'h' * num_cols, data, 3)
        if ps.stream is None:
            raise InterfaceError("An input stream is required for the COPY IN response.")

        bffr: bytearray = bytearray(8192)
        while True:
            bytes_read = ps.stream.readinto(bffr)
            if bytes_read == 0:
                break
            self._write(COPY_DATA + i_pack(bytes_read + 4))
            self._write(bffr[:bytes_read])
            self._flush()

        # Send CopyDone
        # Byte1('c') - Identifier.
        # Int32(4) - Message length, including self.
        self._write(COPY_DONE_MSG)
        self._write(SYNC_MSG)
        self._flush()

    def handle_NOTIFICATION_RESPONSE(self: "Connection", data, ps):
        """
        Handler for NotificationResponse message received via Amazon Redshift wire protocol, represented by
        b'A' code. A message sent if this connection receives a NOTIFY that it was listening for.

        NotificationResponse (B)
            Byte1('A')
                Identifies the message as a notification response.

            Int32
                Length of message contents in bytes, including self.

            Int32
                The process ID of the notifying backend process.

            String
                The name of the condition that the notify has been raised on.

            String
                Additional information passed from the notifying process. (Currently, this feature is unimplemented so the field is always an empty string.)

        Parameters
        ----------
        :param data: bytes:
            Message content
        :param ps: typing.Optional[typing.Dict[str, typing.Any]]:
            Prepared Statement from associated Cursor

        Returns
        -------
        None:None
        """
        backend_pid = i_unpack(data)[0]
        idx: int = 4
        null: int = data.find(NULL_BYTE, idx) - idx
        condition: str = data[idx : idx + null].decode("ascii")
        idx += null + 1
        null = data.find(NULL_BYTE, idx) - idx
        # additional_info = data[idx:idx + null]

        self.notifications.append((backend_pid, condition))

    def cursor(self: "Connection") -> Cursor:
        """Creates a :class:`Cursor` object bound to this
        connection.

        This function is part of the `DBAPI 2.0 specification
        <http://www.python.org/dev/peps/pep-0249/>`_.

        Returns
        -------
        A Cursor object associated with the current Connection: :class:`Cursor`
        """
        return Cursor(self)

    @property
    def description(self: "Connection") -> typing.Optional[typing.List]:
        return self._run_cursor._getDescription()

    def run(self: "Connection", sql, stream=None, **params) -> typing.Tuple[typing.Any, ...]:
        """
        Executes an sql statement, and returns the results as a `tuple`.

        Returns
        -------
        Result of executing an sql statement:tuple[Any, ...]
        """
        self._run_cursor.execute(sql, params, stream=stream)
        return tuple(self._run_cursor._cached_rows)

    def commit(self: "Connection") -> None:
        """Commits the current database transaction.

        This function is part of the `DBAPI 2.0 specification
        <http://www.python.org/dev/peps/pep-0249/>`_.

        Returns
        -------
        None:None
        """
        self.execute(self._cursor, "commit", None)

    def rollback(self: "Connection") -> None:
        """Rolls back the current database transaction.

        This function is part of the `DBAPI 2.0 specification
        <http://www.python.org/dev/peps/pep-0249/>`_.

        Returns
        -------
        None:None
        """
        if not self.in_transaction:
            return
        self.execute(self._cursor, "rollback", None)

    def close(self: "Connection") -> None:
        """Closes the database connection.

        This function is part of the `DBAPI 2.0 specification
        <http://www.python.org/dev/peps/pep-0249/>`_.

        Returns
        -------
        None:None
        """
        try:
            # Byte1('X') - Identifies the message as a terminate message.
            # Int32(4) - Message length, including self.
            self._write(TERMINATE_MSG)
            self._flush()
            if self._sock is not None:
                self._sock.close()
        except AttributeError:
            raise InterfaceError("connection is closed")
        except ValueError:
            raise InterfaceError("connection is closed")
        except socket.error:
            pass
        finally:
            self._usock.close()
            self._sock = None

    def handle_AUTHENTICATION_REQUEST(self: "Connection", data: bytes, cursor: Cursor) -> None:
        """
        Handler for AuthenticationRequest message received via Amazon Redshift wire protocol, represented by
        b'R' code.

        AuthenticationRequest (B)
            Byte1('R')
                Identifies the message as an authentication request.
            Int32(8)
                Length of message contents in bytes, including self.
            Int32(1)
                An authentication code that represents different authentication messages:
                  0 = AuthenticationOk
                  5 = MD5 pwd
                  2 = Kerberos v5 (not supported)
                  3 = Cleartext pwd
                  4 = crypt() pwd (not supported)
                  6 = SCM credential (not supported)
                  7 = GSSAPI (not supported)
                  8 = GSSAPI data (not supported)
                  9 = SSPI (not supported)
                  14 = Redshift Native IDP Integration

        Please note that some authentication messages have additional data following the authentication code.
        That data is documented in the appropriate conditional branch below.

        Parameters
        ----------
        :param data: bytes:
            Message content
        :param cursor: `Cursor`
            The `Cursor` object associated with the given statements execution.

        Returns
        -------
        None:None
        """
        auth_code: int = i_unpack(data)[0]
        if auth_code == 0:
            pass
        elif auth_code == 3:
            if self.password is None:
                raise InterfaceError("server requesting password authentication, but no " "password was provided")
            self._send_message(PASSWORD, self.password + NULL_BYTE)
            self._flush()
        elif auth_code == 5:
            ##
            # A message representing the backend requesting an MD5 hashed
            # password response.  The response will be sent as
            # md5(md5(pwd + login) + salt).

            # Additional message data:
            #  Byte4 - Hash salt.
            salt: bytes = b"".join(cccc_unpack(data, 4))
            if self.password is None:
                raise InterfaceError("server requesting MD5 password authentication, but no " "password was provided")
            pwd: bytes = b"md5" + md5(
                md5(self.password + self.user).hexdigest().encode("ascii") + salt
            ).hexdigest().encode("ascii")
            # Byte1('p') - Identifies the message as a password message.
            # Int32 - Message length including self.
            # String - The password.  Password may be encrypted.
            self._send_message(PASSWORD, pwd + NULL_BYTE)
            self._flush()

        elif auth_code == 10:
            # AuthenticationSASL
            mechanisms: typing.List[str] = [m.decode("ascii") for m in data[4:-1].split(NULL_BYTE)]

            self.auth: ScramClient = ScramClient(mechanisms, self.user.decode("utf8"), self.password.decode("utf8"))

            init: bytes = self.auth.get_client_first().encode("utf8")

            # SASLInitialResponse
            self._write(create_message(PASSWORD, b"SCRAM-SHA-256" + NULL_BYTE + i_pack(len(init)) + init))
            self._flush()

        elif auth_code == 11:
            # AuthenticationSASLContinue
            self.auth.set_server_first(data[4:].decode("utf8"))

            # SASLResponse
            msg: bytes = self.auth.get_client_final().encode("utf8")
            self._write(create_message(PASSWORD, msg))
            self._flush()

        elif auth_code == 12:
            # AuthenticationSASLFinal
            self.auth.set_server_final(data[4:].decode("utf8"))
        elif auth_code == 14:
            # Redshift Native IDP Integration
            aad_token: str = typing.cast(str, self.web_identity_token)
            _logger.debug("<=BE Authentication request IDP")

            if not aad_token:
                raise ConnectionAbortedError(
                    "The server requested AAD token-based authentication, but no token was provided."
                )

            _logger.debug("FE=> IDP(AAD Token)")

            token: bytes = aad_token.encode(encoding="utf-8")
            self._write(create_message(b"i", token))
            # self._write(NULL_BYTE)
            self._flush()

        elif auth_code == 13:  # AUTH_REQ_DIGEST
            offset: int = 4
            algo: int = i_unpack(data, offset)[0]
            algo_names: typing.Tuple[str] = ("SHA256",)
            offset += 4

            salt_len: int = i_unpack(data, offset)[0]
            offset += 4

            salt = data[offset : offset + salt_len]
            offset += salt_len

            server_nonce_len: int = i_unpack(data, offset)[0]
            offset += 4

            server_nonce: bytes = data[offset : offset + server_nonce_len]
            offset += server_nonce_len

            ms_since_epoch: int = int((Datetime.utcnow() - Datetime.utcfromtimestamp(0)).total_seconds() * 1000.0)
            client_nonce: bytes = str(ms_since_epoch).encode("utf-8")

            _logger.debug("handle_AUTHENTICATION_REQUEST: AUTH_REQ_DIGEST")
            _logger.debug("Algo:{}".format(algo))

            if self.password is None:
                raise InterfaceError(
                    "The server requested password-based authentication, but no password was provided."
                )

            if algo > len(algo_names):
                raise InterfaceError(
                    "The server requested password-based authentication, "
                    "but requested algorithm {} is not supported.".format(algo)
                )

            from redshift_connector.utils.extensible_digest import ExtensibleDigest

            digest: bytes = ExtensibleDigest.encode(
                client_nonce=client_nonce,
                password=typing.cast(bytes, self.password),
                salt=salt,
                algo_name=algo_names[algo],
                server_nonce=server_nonce,
            )

            _logger.debug("Password(extensible digest)")

            self._write(b"d")
            self._write(i_pack(4 + 4 + len(digest) + 4 + len(client_nonce)))
            self._write(i_pack(len(digest)))
            self._write(digest)
            self._write(i_pack(len(client_nonce)))
            self._write(client_nonce)
            self._flush()

        elif auth_code in (2, 4, 6, 7, 8, 9):
            raise InterfaceError("Authentication method " + str(auth_code) + " not supported by redshift_connector.")
        else:
            raise InterfaceError("Authentication method " + str(auth_code) + " not recognized by redshift_connector.")

    def handle_READY_FOR_QUERY(self: "Connection", data: bytes, ps) -> None:
        """
        Handler for ReadyForQuery message received via Amazon Redshift wire protocol, represented by b'Z' code.

        ReadyForQuery (B)
            Byte1('Z')
                Identifies the message type. ReadyForQuery is sent whenever the backend is ready for a new query cycle.

            Int32(5)
                Length of message contents in bytes, including self.

            Byte1
                Current backend transaction status indicator. Possible values are 'I' if idle (not in a transaction block); 'T' if in a transaction block; or 'E' if in a failed transaction block (queries will be rejected until block is ended).

        Parameters
        ----------
        :param data: bytes:
            Message content
        :param ps: typing.Optional[typing.Dict[str, typing.Any]]:
            Prepared Statement from associated Cursor

        Returns
        -------
        None:None
        """
        # Byte1 -   Status indicator.
        self.in_transaction = data != IDLE

    def handle_BACKEND_KEY_DATA(self: "Connection", data: bytes, ps) -> None:
        self._backend_key_data = data

    def inspect_datetime(self: "Connection", value: Datetime):
        if value.tzinfo is None:
            return self.py_types[TIMESTAMP]  # timestamp
        else:
            return self.py_types[TIMESTAMPTZ]  # send as timestamptz

    def inspect_int(self: "Connection", value: int):
        if min_int2 < value < max_int2:
            return self.py_types[SMALLINT]
        if min_int4 < value < max_int4:
            return self.py_types[INTEGER]
        if min_int8 < value < max_int8:
            return self.py_types[BIGINT]
        return self.py_types[Decimal]

    def make_params(self: "Connection", values):
        params = []
        for value in values:
            typ = type(value)
            try:
                params.append(self.py_types[typ])
            except KeyError:
                try:
                    params.append(self.inspect_funcs[typ](value))
                except KeyError as e:
                    param = None
                    for k, v in self.py_types.items():
                        try:
                            if isinstance(value, typing.cast(type, k)):
                                param = v
                                break
                        except TypeError:
                            pass

                    if param is None:
                        for k, v in self.inspect_funcs.items():  # type: ignore
                            try:
                                if isinstance(value, k):
                                    v_func: typing.Callable = typing.cast(typing.Callable, v)
                                    param = v_func(value)
                                    break
                            except TypeError:
                                pass
                            except KeyError:
                                pass

                    if param is None:
                        raise NotSupportedError("type " + str(e) + " not mapped to pg type")
                    else:
                        params.append(param)

        return tuple(params)

    def handle_ROW_DESCRIPTION(self: "Connection", data, cursor: Cursor) -> None:
        """
        Handler for RowDescription message received via Amazon Redshift wire protocol, represented by b'T' code.
        Sets ``Connection.ps`` to store metadata.

        RowDescription (B)
            Byte1('T')
                Identifies the message as a row description.

            Int32
                Length of message contents in bytes, including self.

            Int16
                Specifies the number of fields in a row (may be zero).

                Then, for each field, there is the following:

            String
                The field name.

            Int32
                If the field can be identified as a column of a specific table, the object ID of the table; otherwise zero.

            Int16
                If the field can be identified as a column of a specific table, the attribute number of the column; otherwise zero.

            Int32
                The object ID of the field's data type.

            Int16
                The data type size (see pg_type.typlen). Note that negative values denote variable-width types.

            Int32
                The type modifier (see pg_attribute.atttypmod). The meaning of the modifier is type-specific.

            Int16
                The format code being used for the field. Currently will be zero (text) or one (binary). In a RowDescription returned from the statement variant of Describe, the format code is not yet known and will always be zero.

        Parameters
        ----------
        :param data: bytes:
            Message content
        :param cursor: `Cursor`
            The `Cursor` object associated with the given statements execution.

        Returns
        -------
        None:None
        """
        if cursor.ps is None:
            raise InterfaceError("Cursor is missing prepared statement")
        elif "row_desc" not in cursor.ps:
            raise InterfaceError("Prepared Statement is missing row description")

        count: int = h_unpack(data)[0]
        _logger.debug("field count={}".format(count))
        idx = 2
        for i in range(count):
            column_label = data[idx : data.find(NULL_BYTE, idx)]
            idx += len(column_label) + 1

            field: typing.Dict = dict(
                zip(
                    ("table_oid", "column_attrnum", "type_oid", "type_size", "type_modifier", "format"),
                    ihihih_unpack(data, idx),
                )
            )
            field["label"] = column_label
            idx += 18

            if self._client_protocol_version >= ClientProtocolVersion.EXTENDED_RESULT_METADATA:
                for entry in ("schema_name", "table_name", "column_name", "catalog_name"):
                    field[entry] = data[idx : data.find(NULL_BYTE, idx)]
                    idx += len(field[entry]) + 1

                temp: int = h_unpack(data, idx)[0]
                field["nullable"] = temp & 0x1
                field["autoincrement"] = (temp >> 4) & 0x1
                field["read_only"] = (temp >> 8) & 0x1
                field["searchable"] = (temp >> 12) & 0x1
                idx += 2

            cursor.ps["row_desc"].append(field)
            field["pg8000_fc"], field["func"] = self.pg_types[field["type_oid"]]

        _logger.debug(cursor.ps["row_desc"])

    def execute(self: "Connection", cursor: Cursor, operation: str, vals) -> None:
        """
        Executes a database operation. Parameters may be provided as a sequence, or as a mapping, depending upon the value of `redshift_connector.paramstyle`.

        Parameters
        ----------
        cursor : :class:`Cursor`
        operation : str The SQL statement to execute.
        vals : If `redshift_connector.paramstyle` is `qmark`, `numeric`, or `format` this argument should be an array of parameters to bind into the statement. If `redshift_connector.paramstyle` is `named` the argument should be a `dict` mapping of parameters. If `redshift_connector.paramstyle` is `pyformat`, the argument value may be either an array or mapping.

        Returns
        -------
        None:None
        """
        if vals is None:
            vals = ()

        # get the process ID of the calling process.
        pid: int = getpid()
        # multi dimensional dictionary to store the data
        # cache = self._caches[cursor.paramstyle][pid]
        # cache = {'statement': {}, 'ps': {}}
        # statement store the data of statement, ps store the data of prepared statement
        # statement = {operation(query): tuple from 'conver_paramstyle'(statement, make_args)}
        try:
            cache = self._caches[cursor.paramstyle][pid]
        except KeyError:
            try:
                param_cache = self._caches[cursor.paramstyle]
            except KeyError:
                param_cache = self._caches[cursor.paramstyle] = {}

            try:
                cache = param_cache[pid]
            except KeyError:
                cache = param_cache[pid] = {"statement": {}, "ps": {}}

        try:
            statement, make_args = cache["statement"][operation]
        except KeyError:
            statement, make_args = cache["statement"][operation] = convert_paramstyle(cursor.paramstyle, operation)

        args = make_args(vals)
        # change the args to the format that the DB will identify
        # take reference from self.py_types
        params = self.make_params(args)
        key = operation, params

        try:
            ps = cache["ps"][key]
            cursor.ps = ps
        except KeyError:
            statement_nums: typing.List[int] = [0]
            for style_cache in self._caches.values():
                try:
                    pid_cache = style_cache[pid]
                    for csh in pid_cache["ps"].values():
                        statement_nums.append(csh["statement_num"])
                except KeyError:
                    pass

            # statement_num is the id of statement increasing from 1
            statement_num: int = sorted(statement_nums)[-1] + 1
            # consist of "redshift_connector", statement, process id and statement number.
            # e.g redshift_connector_statement_11432_2
            statement_name: str = "_".join(("redshift_connector", "statement", str(pid), str(statement_num)))
            statement_name_bin: bytes = statement_name.encode("ascii") + NULL_BYTE
            # row_desc: list that used to store metadata of rows from DB
            # param_funcs: type transform function
            ps = {
                "statement_name_bin": statement_name_bin,
                "pid": pid,
                "statement_num": statement_num,
                "row_desc": [],
                "param_funcs": tuple(x[2] for x in params),
            }
            cursor.ps = ps

            param_fcs = tuple(x[1] for x in params)

            # Byte1('P') - Identifies the message as a Parse command.
            # Int32 -   Message length, including self.
            # String -  Prepared statement name. An empty string selects the
            #           unnamed prepared statement.
            # String -  The query string.
            # Int16 -   Number of parameter data types specified (can be zero).
            # For each parameter:
            #   Int32 - The OID of the parameter data type.
            val: typing.Union[bytes, bytearray] = bytearray(statement_name_bin)
            typing.cast(bytearray, val).extend(statement.encode(_client_encoding) + NULL_BYTE)
            typing.cast(bytearray, val).extend(h_pack(len(params)))
            for oid, fc, send_func in params:
                # Parse message doesn't seem to handle the -1 type_oid for NULL
                # values that other messages handle.  So we'll provide type_oid
                # 705, the PG "unknown" type.
                typing.cast(bytearray, val).extend(i_pack(705 if oid == -1 else oid))

            # Byte1('D') - Identifies the message as a describe command.
            # Int32 - Message length, including self.
            # Byte1 - 'S' for prepared statement, 'P' for portal.
            # String - The name of the item to describe.

            # PARSE message will notify database to create a prepared statement object
            self._send_message(PARSE, val)
            # DESCRIBE message will specify the name of the existing prepared statement
            # the response will be a parameterDescribing message describe the parameters needed
            # and a RowDescription message describe the rows will be return(nodata message when no return rows)
            self._send_message(DESCRIBE, STATEMENT + statement_name_bin)
            # at completion of query message, driver issue a sync message
            self._write(SYNC_MSG)

            try:
                self._flush()
            except AttributeError as e:
                if self._sock is None:
                    raise InterfaceError("connection is closed")
                else:
                    raise e

            self.handle_messages(cursor)

            # We've got row_desc that allows us to identify what we're
            # going to get back from this statement.
            output_fc = tuple(self.pg_types[f["type_oid"]][0] for f in ps["row_desc"])

            ps["input_funcs"] = tuple(f["func"] for f in ps["row_desc"])
            # Byte1('B') - Identifies the Bind command.
            # Int32 - Message length, including self.
            # String - Name of the destination portal.
            # String - Name of the source prepared statement.
            # Int16 - Number of parameter format codes.
            # For each parameter format code:
            #   Int16 - The parameter format code.
            # Int16 - Number of parameter values.
            # For each parameter value:
            #   Int32 - The length of the parameter value, in bytes, not
            #           including this length.  -1 indicates a NULL parameter
            #           value, in which no value bytes follow.
            #   Byte[n] - Value of the parameter.
            # Int16 - The number of result-column format codes.
            # For each result-column format code:
            #   Int16 - The format code.
            ps["bind_1"] = (
                NULL_BYTE
                + statement_name_bin
                + h_pack(len(params))
                + pack("!" + "h" * len(param_fcs), *param_fcs)
                + h_pack(len(params))
            )

            ps["bind_2"] = h_pack(len(output_fc)) + pack("!" + "h" * len(output_fc), *output_fc)

            if len(cache["ps"]) > self.max_prepared_statements:
                for p in cache["ps"].values():
                    self.close_prepared_statement(p["statement_name_bin"])
                cache["ps"].clear()

            cache["ps"][key] = ps

        cursor._cached_rows.clear()
        cursor._row_count = -1
        cursor._redshift_row_count = -1

        # Byte1('B') - Identifies the Bind command.
        # Int32 - Message length, including self.
        # String - Name of the destination portal.
        # String - Name of the source prepared statement.
        # Int16 - Number of parameter format codes.
        # For each parameter format code:
        #   Int16 - The parameter format code.
        # Int16 - Number of parameter values.
        # For each parameter value:
        #   Int32 - The length of the parameter value, in bytes, not
        #           including this length.  -1 indicates a NULL parameter
        #           value, in which no value bytes follow.
        #   Byte[n] - Value of the parameter.
        # Int16 - The number of result-column format codes.
        # For each result-column format code:
        #   Int16 - The format code.
        retval: bytearray = bytearray(ps["bind_1"])
        for value, send_func in zip(args, ps["param_funcs"]):
            if value is None:
                val = NULL
            else:
                val = send_func(value)
                retval.extend(i_pack(len(val)))
            retval.extend(val)
        retval.extend(ps["bind_2"])

        # send BIND message which includes name of parepared statement,
        # name of destination portal and the value of placeholders in prepared statement.
        # these parameters need to match the prepared statements
        self._send_message(BIND, retval)
        self.send_EXECUTE(cursor)
        self._write(SYNC_MSG)
        self._flush()
        # handle multi messages including BIND_COMPLETE, DATA_ROW, COMMAND_COMPLETE
        # READY_FOR_QUERY
        if self.merge_socket_read:
            self.handle_messages_merge_socket_read(cursor)
        else:
            self.handle_messages(cursor)

    def _send_message(self: "Connection", code: bytes, data: bytes) -> None:
        try:
            self._write(code)
            self._write(i_pack(len(data) + 4))
            self._write(data)
            self._write(FLUSH_MSG)
        except ValueError as e:
            if str(e) == "write to closed file":
                raise InterfaceError("connection is closed")
            else:
                raise e
        except AttributeError:
            raise InterfaceError("connection is closed")

    def send_EXECUTE(self: "Connection", cursor: Cursor) -> None:
        """
        Sends an Execute message in ordinance with Amazon Redshift wire protocol.

        Execute (F)
            Byte1('E')
                Identifies the message as an Execute command.

            Int32
                Length of message contents in bytes, including self.

            String
                The name of the portal to execute (an empty string selects the unnamed portal).

            Int32
                Maximum number of rows to return, if portal contains a query that returns rows (ignored otherwise). Zero denotes "no limit".

        Parameters
        ----------
        :param cursor: `Cursor`
            The `Cursor` object associated with the given statements execution.

        Returns
        -------
        None:None
        """
        self._write(EXECUTE_MSG)
        self._write(FLUSH_MSG)

    def handle_NO_DATA(self: "Connection", msg, ps) -> None:
        """
        Handler for NoData message received via Amazon Redshift wire protocol, represented by b'B' code. Currently a no-op.

        NoData (B)
            Byte1('n')
                Identifies the message as a no-data indicator.

            Int32(4)
                Length of message contents in bytes, including self.

        Parameters
        ----------
        :param msg: bytes:
            Message content
        :param ps: typing.Optional[typing.Dict[str, typing.Any]]:
            Prepared Statement from associated Cursor

        Returns
        -------
        None:None
        """
        pass

    def handle_COMMAND_COMPLETE(self: "Connection", data: bytes, cursor: Cursor) -> None:
        """
        Handler for CommandComplete message received via Amazon Redshift wire protocol, represented by b'C' code.
        Modifies the cursor object and prepared statement.

        CommandComplete (B)
            Byte1('C')
                Identifies the message as a command-completed response.

            Int32
                Length of message contents in bytes, including self.

            String
                The command tag. This is usually a single word that identifies which SQL command was completed.

                For an INSERT command, the tag is INSERT oid rows, where rows is the number of rows inserted. oid is the object ID of the inserted row if rows is 1 and the target table has OIDs; otherwise oid is 0.

                For a DELETE command, the tag is DELETE rows where rows is the number of rows deleted.

                For an UPDATE command, the tag is UPDATE rows where rows is the number of rows updated.

                For a MOVE command, the tag is MOVE rows where rows is the number of rows the cursor's position has been changed by.

                For a FETCH command, the tag is FETCH rows where rows is the number of rows that have been retrieved from the cursor.

        Parameters
        ----------
        :param data: bytes:
            Message content
        :param cursor: `Cursor`
            The `Cursor` object associated with the given statements execution.

        Returns
        -------
        None:None
        """
        values: typing.List[bytes] = data[:-1].split(b" ")
        command = values[0]
        if command in self._commands_with_count:
            row_count: int = int(values[-1])
            if cursor._row_count == -1:
                cursor._row_count = row_count
            else:
                cursor._row_count += row_count
            cursor._redshift_row_count = cursor._row_count
        elif command == b"SELECT":
            # Redshift server does not support row count for SELECT statement
            # so we derive this from the size of the rows associated with the
            # cursor object
            cursor._redshift_row_count = len(cursor._cached_rows)

        if command in (b"ALTER", b"CREATE"):
            for scache in self._caches.values():
                for pcache in scache.values():
                    for ps in pcache["ps"].values():
                        self.close_prepared_statement(ps["statement_name_bin"])
                    pcache["ps"].clear()

    def handle_DATA_ROW(self: "Connection", data: bytes, cursor: Cursor) -> None:
        """
        Handler for DataRow message received via Amazon Redshift wire protocol, represented by b'D' code. Processes
        incoming data rows from Amazon Redshift into Python data types, storing the transformed row in the cursor
        object's `_cached_rows`.

        NoData (B)
            Byte1('n')
                Identifies the message as a no-data indicator.

            Int32(4)
                Length of message contents in bytes, including self.

        Parameters
        ----------
        :param data: bytes:
            Message content
        :param cursor: `Cursor`
            The `Cursor` object associated with the given statements execution.

        Returns
        -------
        None:None
        """
        data_idx: int = 2
        row: typing.List = []
        for desc in cursor.truncated_row_desc():
            vlen: int = i_unpack(data, data_idx)[0]
            data_idx += 4
            if vlen == -1:
                row.append(None)
            elif desc[0] == numeric_in_binary:
                row.append(desc[0](data, data_idx, vlen, desc[1]))
                data_idx += vlen
            else:
                row.append(desc[0](data, data_idx, vlen))
                data_idx += vlen
        cursor._cached_rows.append(row)

    def handle_messages(self: "Connection", cursor: Cursor) -> None:
        """
        Reads messages formatted in ordinance with Amazon Redshift wire protocol, modifying the connection and cursor.

        Parameters
        ----------
        :param cursor: `Cursor`
            The `Cursor` object associated with the given connection object.

        Returns
        -------
        None:None
        """
        code = self.error = None

        while code != READY_FOR_QUERY:
            code, data_len = ci_unpack(self._read(5))
            self.message_types[code](self._read(data_len - 4), cursor)

        if self.error is not None:
            raise self.error

    def handle_messages_merge_socket_read(self: "Connection", cursor: Cursor):
        """
        An optimized version of :func:`Connection.handle_messages` which reduces reads.

        Parameters
        ----------
        :param cursor: `Cursor`
            The `Cursor` object associated with the given connection object.

        Returns
        -------
        None:None
        """
        code = self.error = None
        # read 5 bytes of message firstly
        code, data_len = ci_unpack(self._read(5))

        while True:
            if code == READY_FOR_QUERY:
                # for last message
                self.message_types[code](self._read(data_len - 4), cursor)
                break
            else:
                # read data body of last message and read next 5 bytes of next message
                data = self._read(data_len - 4 + 5)
                last_message_body = data[0:-5]
                self.message_types[code](last_message_body, cursor)
                code, data_len = ci_unpack(data[-5:])

        if self.error is not None:
            raise self.error

    def close_prepared_statement(self: "Connection", statement_name_bin: bytes) -> None:
        """
        Handler for Close message received via Amazon Redshift wire protocol, represented by b'C' code. Clears attributes
        associated with the prepared statement from the current connection object.

        Close (F)
            Byte1('C')
                Identifies the message as a Close command.

            Int32
                Length of message contents in bytes, including self.

            Byte1
                'S' to close a prepared statement; or 'P' to close a portal.

            String
                The name of the prepared statement or portal to close (an empty string selects the unnamed prepared statement or portal).

        Parameters
        ----------
        :param statement_name_bin: bytes:
            Message content

        Returns
        -------
        None:None
        """
        self._send_message(CLOSE, STATEMENT + statement_name_bin)
        self._write(SYNC_MSG)
        self._flush()
        self.handle_messages(self._cursor)

    def handle_NOTICE_RESPONSE(self: "Connection", data: bytes, ps) -> None:
        """
        Handler for NoticeResponse message received via Amazon Redshift wire protocol, represented by b'N' code. Adds the
        received notice to ``Connection.notices``.

        NoticeResponse (B)
            Byte1('N')
                Identifies the message as a notice.

            Int32
                Length of message contents in bytes, including self.

                The message body consists of one or more identified fields, followed by a zero byte as a terminator. Fields may appear in any order. For each field there is the following:

            Byte1
                A code identifying the field type; if zero, this is the message terminator and no string follows. The presently defined field types are listed in Section 42.5. Since more field types may be added in future, frontends should silently ignore fields of unrecognized type.

            String
                The field value.

        Parameters
        ----------
        :param data: bytes:
            Message content
        :param ps: typing.Optional[typing.Dict[str, typing.Any]]:
            Prepared Statement from associated Cursor

        Returns
        -------
        None:None
        """
        self.notices.append(dict((s[0:1], s[1:]) for s in data.split(NULL_BYTE)))

    def handle_PARAMETER_STATUS(self: "Connection", data: bytes, ps) -> None:
        """
        Handler for ParameterStatus message received via Amazon Redshift wire protocol, represented by b'S' code. Modifies
        the connection object inline with parameter values received in preperation for statment execution.

        ParameterStatus (B)
            Byte1('S')
                Identifies the message as a run-time parameter status report.

            Int32
                Length of message contents in bytes, including self.

            String
                The name of the run-time parameter being reported.

            String
                The current value of the parameter.

        Parameters
        ----------
        :param statement_name_bin: bytes:
            Message content

        Returns
        -------
        None:None
        """
        pos: int = data.find(NULL_BYTE)
        key, value = data[:pos], data[pos + 1 : -1]
        self.parameter_statuses.append((key, value))
        if key == b"client_encoding":
            encoding = value.decode("ascii").lower()
            _client_encoding = pg_to_py_encodings.get(encoding, encoding)
        elif key == b"server_protocol_version":
            # when a mismatch occurs between the client's requested protocol version, and the server's response,
            # warn the user and follow server
            if self._client_protocol_version != int(value):
                _logger.debug(
                    "Server indicated {} transfer protocol will be used rather than protocol requested by client: {}".format(
                        ClientProtocolVersion.get_name(int(value)),
                        ClientProtocolVersion.get_name(self._client_protocol_version),
                    )
                )
                self._client_protocol_version = int(value)
                self._enable_protocol_based_conversion_funcs()
        elif key == b"server_version":
            self._server_version: LooseVersion = LooseVersion(value.decode("ascii"))
            if self._server_version < LooseVersion("8.2.0"):
                self._commands_with_count = (b"INSERT", b"DELETE", b"UPDATE", b"MOVE")
            elif self._server_version < LooseVersion("9.0.0"):
                self._commands_with_count = (b"INSERT", b"DELETE", b"UPDATE", b"MOVE", b"FETCH", b"COPY")

    def array_inspect(self: "Connection", value):
        # Check if array has any values. If empty, we can just assume it's an
        # array of strings
        first_element = array_find_first_element(value)
        if first_element is None:
            oid: int = 25
            # Use binary ARRAY format to avoid having to properly
            # escape text in the array literals
            fc: int = FC_BINARY
            array_oid: int = pg_array_types[oid]
        else:
            # supported array output
            typ: type = type(first_element)

            if issubclass(typ, int):
                # special int array support -- send as smallest possible array
                # type
                typ = int
                int2_ok, int4_ok, int8_ok = True, True, True
                for v in array_flatten(value):
                    if v is None:
                        continue
                    if min_int2 < v < max_int2:
                        continue
                    int2_ok = False
                    if min_int4 < v < max_int4:
                        continue
                    int4_ok = False
                    if min_int8 < v < max_int8:
                        continue
                    int8_ok = False
                if int2_ok:
                    array_oid = 1005  # INT2[]
                    oid, fc, send_func = (21, FC_BINARY, h_pack)
                elif int4_ok:
                    array_oid = 1007  # INT4[]
                    oid, fc, send_func = (23, FC_BINARY, i_pack)
                elif int8_ok:
                    array_oid = 1016  # INT8[]
                    oid, fc, send_func = (20, FC_BINARY, q_pack)
                else:
                    raise ArrayContentNotSupportedError("numeric not supported as array contents")
            else:
                try:
                    oid, fc, send_func = self.make_params((first_element,))[0]

                    # If unknown or string, assume it's a string array
                    if oid in (705, 1043, 25):
                        oid = 25
                        # Use binary ARRAY format to avoid having to properly
                        # escape text in the array literals
                        fc = FC_BINARY
                    array_oid = pg_array_types[oid]
                except KeyError:
                    raise ArrayContentNotSupportedError("oid " + str(oid) + " not supported as array contents")
                except NotSupportedError:
                    raise ArrayContentNotSupportedError("type " + str(typ) + " not supported as array contents")
        if fc == FC_BINARY:

            def send_array(arr: typing.List) -> typing.Union[bytes, bytearray]:
                # check that all array dimensions are consistent
                array_check_dimensions(arr)

                has_null: bool = array_has_null(arr)
                dim_lengths: typing.List[int] = array_dim_lengths(arr)
                data: bytearray = bytearray(iii_pack(len(dim_lengths), has_null, oid))
                for i in dim_lengths:
                    data.extend(ii_pack(i, 1))
                for v in array_flatten(arr):
                    if v is None:
                        data += i_pack(-1)
                    elif isinstance(v, typ):
                        inner_data = send_func(v)
                        data += i_pack(len(inner_data))
                        data += inner_data
                    else:
                        raise ArrayContentNotHomogenousError("not all array elements are of type " + str(typ))
                return data

        else:

            def send_array(arr: typing.List) -> typing.Union[bytes, bytearray]:
                array_check_dimensions(arr)
                ar: typing.List = deepcopy(arr)
                for a, i, v in walk_array(ar):
                    if v is None:
                        a[i] = "NULL"
                    elif isinstance(v, typ):
                        a[i] = send_func(v).decode("ascii")
                    else:
                        raise ArrayContentNotHomogenousError("not all array elements are of type " + str(typ))
                return str(ar).translate(arr_trans).encode("ascii")

        return (array_oid, fc, send_array)

    def xid(self: "Connection", format_id, global_transaction_id, branch_qualifier) -> typing.Tuple:
        """Create a Transaction IDs (only global_transaction_id is used in pg)
        format_id and branch_qualifier are not used in Amazon Redshift
        global_transaction_id may be any string identifier supported by
        Amazon Redshift.

        Returns
        -------
        (format_id, global_transaction_id, branch_qualifier):typing.Tuple
        """
        return (format_id, global_transaction_id, branch_qualifier)

    def tpc_begin(self: "Connection", xid) -> None:
        """Begins a TPC transaction with the given transaction ID xid.

        This method should be called outside of a transaction (i.e. nothing may
        have executed since the last .commit() or .rollback()).

        Furthermore, it is an error to call .commit() or .rollback() within the
        TPC transaction. A ProgrammingError is raised, if the application calls
        .commit() or .rollback() during an active TPC transaction.

        This function is part of the `DBAPI 2.0 specification
        <http://www.python.org/dev/peps/pep-0249/>`_.

        Returns
        -------
        None:None
        """
        self._xid = xid
        if self.autocommit:
            self.execute(self._cursor, "begin transaction", None)

    def tpc_prepare(self: "Connection") -> None:
        """Performs the first phase of a transaction started with .tpc_begin().
        A ProgrammingError is be raised if this method is called outside of a
        TPC transaction.

        After calling .tpc_prepare(), no statements can be executed until
        .tpc_commit() or .tpc_rollback() have been called.

        This function is part of the `DBAPI 2.0 specification
        <http://www.python.org/dev/peps/pep-0249/>`_.

        Returns
        -------
        None:None
        """
        if self._xid is None or len(self._xid) < 2:
            raise InterfaceError("Malformed Transaction Id")

        q: str = "PREPARE TRANSACTION '%s';" % (self._xid[1],)
        self.execute(self._cursor, q, None)

    def tpc_commit(self: "Connection", xid=None) -> None:
        """When called with no arguments, .tpc_commit() commits a TPC
        transaction previously prepared with .tpc_prepare().

        If .tpc_commit() is called prior to .tpc_prepare(), a single phase
        commit is performed. A transaction manager may choose to do this if
        only a single resource is participating in the global transaction.

        When called with a transaction ID xid, the database commits the given
        transaction. If an invalid transaction ID is provided, a
        ProgrammingError will be raised. This form should be called outside of
        a transaction, and is intended for use in recovery.

        On return, the TPC transaction is ended.

        This function is part of the `DBAPI 2.0 specification
        <http://www.python.org/dev/peps/pep-0249/>`_.

        Returns
        -------
        None:None
        """
        if xid is None:
            xid = self._xid

        if xid is None:
            raise ProgrammingError("Cannot tpc_commit() without a TPC transaction!")

        try:
            previous_autocommit_mode: bool = self.autocommit
            self.autocommit = True
            if xid in self.tpc_recover():
                self.execute(self._cursor, "COMMIT PREPARED '%s';" % (xid[1],), None)
            else:
                # a single-phase commit
                self.commit()
        finally:
            self.autocommit = previous_autocommit_mode
        self._xid = None

    def tpc_rollback(self: "Connection", xid=None) -> None:
        """When called with no arguments, .tpc_rollback() rolls back a TPC
        transaction. It may be called before or after .tpc_prepare().

        When called with a transaction ID xid, it rolls back the given
        transaction. If an invalid transaction ID is provided, a
        ProgrammingError is raised. This form should be called outside of a
        transaction, and is intended for use in recovery.

        On return, the TPC transaction is ended.

        This function is part of the `DBAPI 2.0 specification
        <http://www.python.org/dev/peps/pep-0249/>`_.

        Returns
        -------
        None:None
        """
        if xid is None:
            xid = self._xid

        if xid is None:
            raise ProgrammingError("Cannot tpc_rollback() without a TPC prepared transaction!")

        try:
            previous_autocommit_mode: bool = self.autocommit
            self.autocommit = True
            if xid in self.tpc_recover():
                # a two-phase rollback
                self.execute(self._cursor, "ROLLBACK PREPARED '%s';" % (xid[1],), None)
            else:
                # a single-phase rollback
                self.rollback()
        finally:
            self.autocommit = previous_autocommit_mode
        self._xid = None

    def tpc_recover(self: "Connection") -> typing.List[typing.Tuple[typing.Any, ...]]:
        """Returns a list of pending transaction IDs suitable for use with
        .tpc_commit(xid) or .tpc_rollback(xid).

        This function is part of the `DBAPI 2.0 specification
        <http://www.python.org/dev/peps/pep-0249/>`_.

        Returns
        -------
        List of pending transaction IDs:List[tuple[Any, ...]]
        """
        try:
            previous_autocommit_mode: bool = self.autocommit
            self.autocommit = True
            curs = self.cursor()
            curs.execute("select xact_id FROM stl_undone")
            return [self.xid(0, row[0], "") for row in curs]
        finally:
            self.autocommit = previous_autocommit_mode
