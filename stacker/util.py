import copy
import collections
import uuid
import importlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from git import Repo

import botocore.exceptions

logger = logging.getLogger(__name__)


def retry_with_backoff(function, args=None, kwargs=None, attempts=5,
                       min_delay=1, max_delay=3, exc_list=None,
                       retry_checker=None):
    """Retries function, catching expected Exceptions.

    Each retry has a delay between `min_delay` and `max_delay` seconds,
    increasing with each attempt.

    Args:
        function (function): The function to call.
        args (list, optional): A list of positional arguments to pass to the
            given function.
        kwargs (dict, optional): Keyword arguments to pass to the given
            function.
        attempts (int, optional): The # of times to retry the function.
            Default: 5
        min_delay (int, optional): The minimum time to delay retries, in
            seconds. Default: 1
        max_delay (int, optional): The maximum time to delay retries, in
            seconds. Default: 5
        exc_list (list, optional): A list of :class:`Exception` classes that
            should be retried. Default: [:class:`Exception`,]
        retry_checker (func, optional): An optional function that is used to
            do a deeper analysis on the received :class:`Exception` to
            determine if it qualifies for retry. Receives a single argument,
            the :class:`Exception` object that was caught. Should return
            True if it should be retried.

    Returns:
        variable: Returns whatever the given function returns.

    Raises:
        :class:`Exception`: Raises whatever exception the given function
            raises, if unable to succeed within the given number of attempts.
    """
    args = args or []
    kwargs = kwargs or {}
    attempt = 0
    if not exc_list:
        exc_list = (Exception, )
    while True:
        attempt += 1
        logger.debug("Calling %s, attempt %d.", function, attempt)
        sleep_time = min(max_delay, min_delay * attempt)
        try:
            return function(*args, **kwargs)
        except exc_list as e:
            # If there is no retry checker function, or if there is and it
            # returns True, then go ahead and retry
            if not retry_checker or retry_checker(e):
                if attempt == attempts:
                    logger.error("Function %s failed after %s retries. Giving "
                                 "up.", function.func_name, attempts)
                    raise
                logger.debug("Caught expected exception: %r", e)
            # If there is a retry checker function, and it returned False,
            # do not retry
            else:
                raise
        time.sleep(sleep_time)


def camel_to_snake(name):
    """Converts CamelCase to snake_case.

    Args:
        name (string): The name to convert from CamelCase to snake_case.

    Returns:
        string: Converted string.
    """
    s1 = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub("([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def convert_class_name(kls):
    """Gets a string that represents a given class.

    Args:
        kls (class): The class being analyzed for its name.

    Returns:
        string: The name of the given kls.
    """
    return camel_to_snake(kls.__name__)


def parse_zone_id(full_zone_id):
    """Parses the returned hosted zone id and returns only the ID itself."""
    return full_zone_id.split("/")[2]


def get_hosted_zone_by_name(client, zone_name):
    """Get the zone id of an existing zone by name.

    Args:
        client (:class:`botocore.client.Route53`): The connection used to
            interact with Route53's API.
        zone_name (string): The name of the DNS hosted zone to create.

    Returns:
        string: The Id of the Hosted Zone.
    """
    p = client.get_paginator("list_hosted_zones")

    for i in p.paginate():
        for zone in i["HostedZones"]:
            if zone["Name"] == zone_name:
                return parse_zone_id(zone["Id"])
    return None


def get_or_create_hosted_zone(client, zone_name):
    """Get the Id of an existing zone, or create it.

    Args:
        client (:class:`botocore.client.Route53`): The connection used to
            interact with Route53's API.
        zone_name (string): The name of the DNS hosted zone to create.

    Returns:
        string: The Id of the Hosted Zone.
    """
    zone_id = get_hosted_zone_by_name(client, zone_name)
    if zone_id:
        return zone_id

    logger.debug("Zone %s does not exist, creating.", zone_name)

    reference = uuid.uuid4().hex

    response = client.create_hosted_zone(Name=zone_name,
                                         CallerReference=reference)

    return parse_zone_id(response["HostedZone"]["Id"])


class SOARecordText(object):
    """Represents the actual body of an SOARecord. """
    def __init__(self, record_text):
        (self.nameserver, self.contact, self.serial, self.refresh,
            self.retry, self.expire, self.min_ttl) = record_text.split()

    def __str__(self):
        return "%s %s %s %s %s %s %s" % (
            self.nameserver, self.contact, self.serial, self.refresh,
            self.retry, self.expire, self.min_ttl
        )


class SOARecord(object):
    """Represents an SOA record. """
    def __init__(self, record):
        self.name = record["Name"]
        self.text = SOARecordText(record["ResourceRecords"][0]["Value"])
        self.ttl = record["TTL"]


def get_soa_record(client, zone_id, zone_name):
    """Gets the SOA record for zone_name from zone_id.

    Args:
        client (:class:`botocore.client.Route53`): The connection used to
            interact with Route53's API.
        zone_id (string): The AWS Route53 zone id of the hosted zone to query.
        zone_name (string): The name of the DNS hosted zone to create.

    Returns:
        :class:`stacker.util.SOARecord`: An object representing the parsed SOA
            record returned from AWS Route53.
    """

    response = client.list_resource_record_sets(HostedZoneId=zone_id,
                                                StartRecordName=zone_name,
                                                StartRecordType="SOA",
                                                MaxItems="1")
    return SOARecord(response["ResourceRecordSets"][0])


def create_route53_zone(client, zone_name):
    """Creates the given zone_name if it doesn't already exists.

    Also sets the SOA negative caching TTL to something short (300 seconds).

    Args:
        client (:class:`botocore.client.Route53`): The connection used to
            interact with Route53's API.
        zone_name (string): The name of the DNS hosted zone to create.

    Returns:
        string: The zone id returned from AWS for the existing, or newly
            created zone.
    """
    if not zone_name.endswith("."):
        zone_name += "."
    zone_id = get_or_create_hosted_zone(client, zone_name)
    old_soa = get_soa_record(client, zone_id, zone_name)

    # If the negative cache value is already 300, don't update it.
    if old_soa.text.min_ttl == "300":
        return zone_id

    new_soa = copy.deepcopy(old_soa)
    logger.debug("Updating negative caching value on zone %s to 300.",
                 zone_name)
    new_soa.text.min_ttl = "300"
    client.change_resource_record_sets(
        HostedZoneId=zone_id,
        ChangeBatch={
            "Comment": "Update SOA min_ttl to 300.",
            "Changes": [
                {
                    "Action": "UPSERT",
                    "ResourceRecordSet": {
                        "Name": zone_name,
                        "Type": "SOA",
                        "TTL": old_soa.ttl,
                        "ResourceRecords": [
                            {
                                "Value": str(new_soa.text)
                            }
                        ]
                    }
                },
            ]
        }
    )
    return zone_id


def load_object_from_string(fqcn):
    """Converts "." delimited strings to a python object.

    Given a "." delimited string representing the full path to an object
    (function, class, variable) inside a module, return that object.  Example:

    load_object_from_string("os.path.basename")
    load_object_from_string("logging.Logger")
    load_object_from_string("LocalClassName")
    """
    module_path = "__main__"
    object_name = fqcn
    if "." in fqcn:
        module_path, object_name = fqcn.rsplit(".", 1)
        importlib.import_module(module_path)
    return getattr(sys.modules[module_path], object_name)


def uppercase_first_letter(s):
    """Return string "s" with first character upper case."""
    return s[0].upper() + s[1:]


def cf_safe_name(name):
    """Converts a name to a safe string for a Cloudformation resource.

    Given a string, returns a name that is safe for use as a CloudFormation
    Resource. (ie: Only alphanumeric characters)
    """
    alphanumeric = r"[a-zA-Z0-9]+"
    parts = re.findall(alphanumeric, name)
    return "".join([uppercase_first_letter(part) for part in parts])


def handle_hooks(stage, hooks, provider, context):
    """ Used to handle pre/post_build hooks.

    These are pieces of code that we want to run before/after the builder
    builds the stacks.

    Args:
        stage (string): The current stage (pre_run, post_run, etc).
        hooks (list): A list of dictionaries containing the hooks to execute.
        provider (:class:`stacker.provider.base.BaseProvider`): The provider
            the current stack is using.
        context (:class:`stacker.context.Context`): The current stacker
            context.
    """
    if not hooks:
        logger.debug("No %s hooks defined.", stage)
        return

    hook_paths = []
    for i, h in enumerate(hooks):
        try:
            hook_paths.append(h["path"])
        except KeyError:
            raise ValueError("%s hook #%d missing path." % (stage, i))

    logger.info("Executing %s hooks: %s", stage, ", ".join(hook_paths))
    for hook in hooks:
        data_key = hook.get("data_key")
        required = hook.get("required", True)
        kwargs = hook.get("args", {})
        try:
            method = load_object_from_string(hook["path"])
        except (AttributeError, ImportError):
            logger.exception("Unable to load method at %s:", hook["path"])
            if required:
                raise
            continue
        try:
            result = method(context=context, provider=provider, **kwargs)
        except Exception:
            logger.exception("Method %s threw an exception:", hook["path"])
            if required:
                raise
            continue
        if not result:
            if required:
                logger.error("Required hook %s failed. Return value: %s",
                             hook["path"], result)
                sys.exit(1)
            logger.warning("Non-required hook %s failed. Return value: %s",
                           hook["path"], result)
        else:
            if isinstance(result, collections.Mapping):
                if data_key:
                    logger.debug("Adding result for hook %s to context in "
                                 "data_key %s.", hook["path"], data_key)
                    context.set_hook_data(data_key, result)
                else:
                    logger.debug("Hook %s returned result data, but no data "
                                 "key set, so ignoring.", hook["path"])


def get_config_directory():
    """Return the directory the config file is located in.

    This enables us to use relative paths in config values.

    """
    # avoid circular import
    from .commands.stacker import Stacker
    command = Stacker()
    namespace = command.parse_args()
    return os.path.dirname(namespace.config.name)


def read_value_from_path(value):
    """Enables translators to read values from files.

    The value can be referred to with the `file://` prefix. ie:

        conf_key: ${kms file://kms_value.txt}

    """
    if value.startswith('file://'):
        path = value.split('file://', 1)[1]
        config_directory = get_config_directory()
        relative_path = os.path.join(config_directory, path)
        with open(relative_path) as read_file:
            value = read_file.read()
    return value


def get_client_region(client):
    """Gets the region from a :class:`boto3.client.Client` object.

    Args:
        client (:class:`boto3.client.Client`): The client to get the region
            from.

    Returns:
        string: AWS region string.
    """

    return client._client_config.region_name


def get_s3_endpoint(client):
    """Gets the s3 endpoint for the given :class:`boto3.client.Client` object.

    Args:
        client (:class:`boto3.client.Client`): The client to get the endpoint
            from.

    Returns:
        string: The AWS endpoint for the client.
    """

    return client._endpoint.host


def s3_bucket_location_constraint(region):
    """Returns the appropriate LocationConstraint info for a new S3 bucket.

    When creating a bucket in a region OTHER than us-east-1, you need to
    specify a LocationConstraint inside the CreateBucketConfiguration argument.
    This function helps you determine the right value given a given client.

    Args:
        region (str): The region where the bucket will be created in.

    Returns:
        string: The string to use with the given client for creating a bucket.
    """
    if region == "us-east-1":
        return ""
    return region


def ensure_s3_bucket(s3_client, bucket_name, bucket_region):
    """Ensure an s3 bucket exists, if it does not then create it.

    Args:
        s3_client (:class:`botocore.client.Client`): An s3 client used to
            verify and create the bucket.
        bucket_name (str): The bucket being checked/created.
        bucket_region (str, optional): The region to create the bucket in. If
            not provided, will be determined by s3_client's region.
    """
    try:
        s3_client.head_bucket(Bucket=bucket_name)
    except botocore.exceptions.ClientError as e:
        if e.response['Error']['Message'] == "Not Found":
            logger.debug("Creating bucket %s.", bucket_name)
            create_args = {"Bucket": bucket_name}
            location_constraint = s3_bucket_location_constraint(
                bucket_region
            )
            if location_constraint:
                create_args["CreateBucketConfiguration"] = {
                    "LocationConstraint": location_constraint
                }
            s3_client.create_bucket(**create_args)
        elif e.response['Error']['Message'] == "Forbidden":
            logger.exception("Access denied for bucket %s.  Did " +
                             "you remember to use a globally unique name?",
                             bucket_name)
            raise
        else:
            logger.exception("Error creating bucket %s. Error %s",
                             bucket_name, e.response)
            raise


class SourceProcessor():
    """Makes remote python package sources available in the running python
       environment."""

    def __init__(self, stacker_cache_dir=None):
        """
        Processes a config's list of package sources

        Args:
            stacker_cache_dir (string): Directory of stacker local cache.
                                        Default to $HOME/.stacker
        """
        stacker_cache_dir = (stacker_cache_dir or
                             os.path.join(os.environ['HOME'], '.stacker'))
        package_cache_dir = os.path.join(stacker_cache_dir, 'packages')
        self.stacker_cache_dir = stacker_cache_dir
        self.package_cache_dir = package_cache_dir
        self.create_cache_directories()

    def create_cache_directories(self):
        """Ensures that SourceProcessor cache directories exist."""
        if not os.path.isdir(self.package_cache_dir):
            if not os.path.isdir(self.stacker_cache_dir):
                os.mkdir(self.stacker_cache_dir)
            os.mkdir(self.package_cache_dir)

    def get_package_sources(self, sources):
        """Makes remote python packages available for local use

        Args:
            sources (dict): Dictionary of remote sources from config.
                            Currently supports git repositories
            Example:
              {'git': [
                  {'uri': 'git@github.com:remind101/stacker_blueprints.git',
                   'tag': '1.0.0',
                   'paths': ['stacker_blueprints']},
                  {'uri': 'git@github.com:acmecorp/stacker_blueprints.git'},
                  {'uri': 'git@github.com:contoso/webapp.git',
                   'branch': 'staging'},
                  {'uri': 'git@github.com:contoso/foo.git',
                   'commit': '12345678'}
              ]}

        """
        # Checkout git repositories specified in config
        if 'git' in sources:
            for config in sources['git']:
                self.fetch_git_package(config=config)

    def fetch_git_package(self, config):
        """Makes a remote git repository available for local use

        Args:
            config (dict): Dictionary of git repo configuration

        """
        ref = self.determine_git_ref(config)
        dir_name = self.sanitize_git_path(uri=config['uri'], ref=ref)
        cached_dir_path = os.path.join(self.package_cache_dir, dir_name)

        # We can skip cloning the repo if it's already been cached
        if not os.path.isdir(cached_dir_path):
            tmp_dir = tempfile.mkdtemp(prefix='stacker')
            try:
                tmp_repo_path = os.path.join(tmp_dir, dir_name)
                with Repo.clone_from(config['uri'], tmp_repo_path) as repo:
                    repo.head.reference = ref
                    repo.head.reset(index=True, working_tree=True)
                shutil.move(tmp_repo_path, self.package_cache_dir)
            finally:
                shutil.rmtree(tmp_dir)

        # Cloning (if necessary) is complete. Now add the appropriate
        # directory (or directories) to sys.path
        if 'paths' in config:
            for path in config['paths']:
                path_to_append = os.path.join(self.package_cache_dir,
                                              dir_name,
                                              path)
                logger.debug("Appending \"%s\" to python sys.path",
                             path_to_append)
                sys.path.append(os.path.join(self.package_cache_dir,
                                             dir_name,
                                             path))
        else:
            sys.path.append(cached_dir_path)

    def git_ls_remote(self, uri, ref):
        """Determines the latest commit id for a given ref.

        Args:
            uri (string): git URI
            ref (string): git ref

        Returns:
            str: A commit id
        """
        logger.debug("Invoking git to retrieve commit id for repo %s...", uri)
        lsremote_output = subprocess.check_output(['git',
                                                   'ls-remote',
                                                   uri,
                                                   ref])
        if "\t" in lsremote_output:
            commit_id = lsremote_output.split("\t")[0]
            logger.debug("Matching commit id found: %s", commit_id)
            return commit_id
        else:
            raise ValueError("Ref \"%s\" not found for repo %d." % (ref, uri))

    def determine_git_ls_remote_ref(self, config):
        """Takes a dict describing a git repo and determines the ref to be used
           with the "git ls-remote" command

        Args:
            config (dict): git config dictionary; 'branch' key is optional

        Returns:
            str: A branch reference or "HEAD"
        """
        if 'branch' in config:
            ref = "refs/heads/%s" % config.get('branch')
        else:
            ref = "HEAD"

        return ref

    def determine_git_ref(self, config):
        """Takes a dict describing a git repo and determines the ref to be used
           for 'git checkout'.

        Args:
            config (dict): git config dictionary

        Returns:
            str: A commit id or tag name
        """
        # First ensure redundant config keys aren't specified (which could
        # cause confusion as to which take precedence)
        ref_config_keys = 0
        for i in ['commit', 'tag', 'branch']:
            if config.get(i):
                ref_config_keys += 1
        if ref_config_keys > 1:
            raise ImportError("Fetching remote git sources failed: "
                              "conflicting revisions (e.g. 'commit', 'tag', "
                              "'branch') specified for a package source")

        # Now check for a specific point in time referenced and return it if
        # present
        if config.get('commit'):
            ref = config['commit']
        elif config.get('tag'):
            ref = config['tag']
        else:
            # Since a specific commit/tag point in time has not been specified,
            # check the remote repo for the commit id to use
            ref = self.git_ls_remote(
                config['uri'],
                self.determine_git_ls_remote_ref(config)
            )
        return ref

    def sanitize_git_path(self, uri, ref=None):
        """Takes a git URI and ref and converts it to a directory safe path

        Args:
            uri (string): git URI
                          (e.g. git@github.com:foo/bar.git)
            ref (string): optional git ref to be appended to the path

        Returns:
            str: Directory name for the supplied uri
        """
        if uri.endswith('.git'):
            dir_name = uri[:-4]  # drop .git
        else:
            dir_name = uri
        for i in ['@', '/', ':']:
            dir_name = dir_name.replace(i, '_')
        if ref is not None:
            dir_name += "-%s" % ref
        return dir_name
