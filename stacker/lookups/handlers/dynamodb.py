from stacker.session_cache import get_session

from ...util import read_value_from_path

TYPE_NAME = "dynamodb"


def handler(value, **kwargs):
    """Get a value from a dynamodb table

    dynamodb field types should be in the following format:

        [<region>:<tablename>@]<keyname>.<keyvalue>.<keyvalue>...

    Note: The region is optional, and defaults to the environment's
    `AWS_DEFAULT_REGION` if not specified.



    """
    value = read_value_from_path(value)

    table_info, table_keys = None
    region, table_name = None
    if "@" in value:
        table_info, table_keys = value.split("@", 1)
        if ":" in table_info:
            region, table_name = table_info.split(":", 1)
        else:
            table_name = table_info
    else:
        raise ValueError('Please make sure to include a tablename and region')

    if table_name is None:
        raise ValueError('Please make sure to include a dynamodb table name')

    dynamodb = get_session(region).client('dynamodb')
    response = dynamodb.get_item(
        TableName=table_name,
        Key={
            table_keys[0]: table_keys[1]
        }
    )
    table_keys = table_keys[:2]
    print response
    print table_keys
