import typing

from redshift_connector.config import DEFAULT_PROTOCOL_VERSION
from redshift_connector.error import ProgrammingError

SERVERLESS_HOST_PATTERN: str = r"(.+)\.(.+).redshift-serverless(-dev)?\.amazonaws\.com(.)*"


class RedshiftProperty:
    def __init__(self: "RedshiftProperty", **kwargs):
        """
        Initialize a RedshiftProperty object.
        """
        if not kwargs:
            # The access key for the IAM role or IAM user configured for IAM database authentication
            self.access_key_id: typing.Optional[str] = None
            # This option specifies whether the driver uses the DbUser value from the SAML assertion
            # or the value that is specified in the DbUser connection property in the connection URL.
            self.allow_db_user_override: bool = False
            # The Okta-provided unique ID associated with your Redshift application.
            self.app_id: typing.Optional[str] = None
            # The name of the Okta application that you use to authenticate the connection to Redshift.
            self.app_name: str = "amazon_aws_redshift"
            self.application_name: typing.Optional[str] = None
            self.auth_profile: typing.Optional[str] = None
            # Indicates whether the user should be created if it does not already exist.
            self.auto_create: bool = False
            # The client ID associated with the user name in the Azure AD portal. Only used for Azure AD.
            self.client_id: typing.Optional[str] = None
            # client's requested transfer protocol version. See config.py for supported protocols
            self.client_protocol_version: int = DEFAULT_PROTOCOL_VERSION
            # The client secret as associated with the client ID in the AzureAD portal. Only used for Azure AD.
            self.client_secret: typing.Optional[str] = None
            # The name of the Redshift Cluster to use.
            self.cluster_identifier: typing.Optional[str] = None
            # The class path to a specific credentials provider plugin class.
            self.credentials_provider: typing.Optional[str] = None
            # Boolean indicating if application supports multidatabase datashare catalogs.
            # Default value of True indicates the application is does not support multidatabase datashare
            # catalogs for backwards compatibility.
            self.database_metadata_current_db_only: bool = True
            # A list of existing database group names that the DbUser joins for the current session.
            # If not specified, defaults to PUBLIC.
            self.db_groups: typing.List[str] = list()
            # database name
            self.db_name: str = ""
            # The user name.
            self.db_user: typing.Optional[str] = None
            # The length of time, in seconds
            self.duration: int = 900
            self.endpoint_url: typing.Optional[str] = None
            # Forces the database group names to be lower case.
            self.force_lowercase: bool = False
            # The host to connect to.
            self.host: str = ""
            self.iam: bool = False
            self.iam_disable_cache: bool = False
            # The IdP (identity provider) host you are using to authenticate into Redshift.
            self.idp_host: typing.Optional[str] = None
            # timeout for authentication via Browser IDP
            self.idp_response_timeout: int = 120
            # The Azure AD tenant ID for your Redshift application.Only used for Azure AD.
            self.idp_tenant: typing.Optional[str] = None
            # The port used by an IdP (identity provider).
            self.idpPort: int = 443
            self.listen_port: int = 7890
            self.login_url: typing.Optional[str] = None
            # max number of prepared statements
            self.max_prepared_statements: int = 1000
            # parameter for PingIdentity
            self.partner_sp_id: typing.Optional[str] = None
            # The password.
            self.password: str = ""
            # The port to connect to.
            self.port: int = 5439
            # The IAM role you want to assume during the connection to Redshift.
            self.preferred_role: typing.Optional[str] = None
            # The Amazon Resource Name (ARN) of the SAML provider in IAM that describes the IdP.
            self.principal: typing.Optional[str] = None
            # The name of a profile in a AWS credentials or config file that contains values for connection options
            self.profile: typing.Optional[str] = None
            # The AWS region where the cluster specified by cluster_identifier is located.
            self.region: typing.Optional[str] = None
            # Used to run in streaming replication mode. If your server character encoding is not ascii or utf8,
            # then you need to provide values as bytes
            self.replication: typing.Optional[str] = None
            self.role_arn: typing.Optional[str] = None
            self.role_session_name: typing.Optional[str] = None
            # The secret access key for the IAM role or IAM user configured for IAM database authentication
            self.secret_access_key: typing.Optional[str] = None
            # session_token is required only for an IAM role with temporary credentials.
            # session_token is not used for an IAM user.
            self.session_token: typing.Optional[str] = None
            # The source IP address which initiates the connection to the Amazon Redshift server.
            self.source_address: typing.Optional[str] = None
            # if SSL authentication will be used
            self.ssl: bool = True
            # This property indicates whether the IDP hosts server certificate should be verified.
            self.ssl_insecure: bool = True
            # ssl mode: verify-ca or verify-full.
            self.sslmode: str = "verify-ca"
            # Use this property to enable or disable TCP keepalives.
            self.tcp_keepalive: bool = True
            # This is the time in seconds before the connection to the server will time out.
            self.timeout: typing.Optional[int] = None
            # The path to the UNIX socket to access the database through
            self.unix_sock: typing.Optional[str] = None
            # The user name.
            self.user_name: str = ""
            self.web_identity_token: typing.Optional[str] = None
            # The AWS Account Id
            self.account_id: typing.Optional[str] = None
            # The name of the Redshift Native Auth Provider
            self.provider_name: typing.Optional[str] = None
            self.scope: str = ""

        else:
            for k, v in kwargs.items():
                setattr(self, k, v)

    def __str__(self: "RedshiftProperty") -> str:
        rp = self.__dict__
        rp["is_serverless_host"] = self.is_serverless_host
        return str(rp)

    def put_all(self, other):
        """
        Merges two RedshiftProperty objects overriding pre-defined attributes with the value provided by other, if present.
        """
        from copy import deepcopy

        for k, v in other.__dict__.items():
            setattr(self, k, deepcopy(v))

    def put(self: "RedshiftProperty", key: str, value: typing.Any):
        """
        Sets the value of the specified attribute if the value provided is not None.
        """
        if value is not None:
            setattr(self, key, value)

    @property
    def is_serverless_host(self: "RedshiftProperty") -> bool:
        """
        If the host indicate Redshift serverless will be used for connection.
        """
        if not self.host:
            return False

        import re

        return bool(re.fullmatch(pattern=SERVERLESS_HOST_PATTERN, string=str(self.host)))

    def set_account_id_from_host(self: "RedshiftProperty") -> None:
        """
        Returns the AWS account id as parsed from the Redshift serverless endpoint.
        """
        import re

        m2 = re.fullmatch(pattern=SERVERLESS_HOST_PATTERN, string=self.host)

        if m2:
            self.put(key="account_id", value=m2.group(1))

    def set_region_from_host(self: "RedshiftProperty") -> None:
        """
        Returns the AWS region as parsed from the Redshift serverless endpoint.
        """
        import re

        m2 = re.fullmatch(pattern=SERVERLESS_HOST_PATTERN, string=self.host)

        if m2:
            self.put(key="region", value=m2.group(2))
