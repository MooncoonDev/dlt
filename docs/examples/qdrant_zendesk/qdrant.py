from typing import Optional, Dict, Any, Tuple

import dlt
from dlt.common import pendulum
from dlt.common.time import ensure_pendulum_datetime
from dlt.common.typing import TAnyDateTime
from dlt.sources.helpers.requests import client
from dlt.destinations.qdrant import qdrant_adapter
from qdrant_client import QdrantClient

from dlt.common.configuration.inject import with_config

# function from: https://github.com/dlt-hub/verified-sources/tree/master/sources/zendesk
@dlt.source(max_table_nesting=2)
def zendesk_support(
    credentials: Dict[str, str] = dlt.secrets.value,
    start_date: Optional[TAnyDateTime] = pendulum.datetime(year=2000, month=1, day=1),  # noqa: B008
    end_date: Optional[TAnyDateTime] = None,
):
    """
    Retrieves data from Zendesk Support for tickets events.

    Args:
        credentials: Zendesk credentials (default: dlt.secrets.value)
        start_date: Start date for data extraction (default: 2000-01-01)
        end_date: End date for data extraction (default: None).
            If end time is not provided, the incremental loading will be
            enabled, and after the initial run, only new data will be retrieved.

    Returns:
        DltResource.
    """
    # Convert start_date and end_date to Pendulum datetime objects
    start_date_obj = ensure_pendulum_datetime(start_date)
    end_date_obj = ensure_pendulum_datetime(end_date) if end_date else None

    # Convert Pendulum datetime objects to Unix timestamps
    start_date_ts = start_date_obj.int_timestamp
    end_date_ts: Optional[int] = None
    if end_date_obj:
        end_date_ts = end_date_obj.int_timestamp

    # Extract credentials from secrets dictionary
    auth = (credentials["email"], credentials["password"])
    subdomain = credentials["subdomain"]
    url = f"https://{subdomain}.zendesk.com"

    # we use `append` write disposition, because objects in tickets_data endpoint are never updated
    #  so we do not need to merge
    # we set primary_key so allow deduplication of events by the `incremental` below in the rare case
    #  when two events have the same timestamp
    @dlt.resource(primary_key="id", write_disposition="append")
    def tickets_data(
        updated_at: dlt.sources.incremental[
            pendulum.DateTime
        ] = dlt.sources.incremental(
            "updated_at",
            initial_value=start_date_obj,
            end_value=end_date_obj,
            allow_external_schedulers=True,
        )
    ):
        # URL For ticket events
        # 'https://d3v-dlthub.zendesk.com/api/v2/incremental/tickets_data.json?start_time=946684800'
        event_pages = get_pages(
            url=url,
            endpoint="/api/v2/incremental/tickets",
            auth=auth,
            data_point_name="tickets",
            params={"start_time": updated_at.last_value.int_timestamp},
        )
        for page in event_pages:
            yield ([_fix_date(ticket) for ticket in page])

            # stop loading when using end_value and end is reached.
            # unfortunately, Zendesk API does not have the "end_time" parameter, so we stop iterating ourselves
            if updated_at.end_out_of_range:
                return

    return tickets_data


# helper function to fix the datetime format
def _parse_date_or_none(value: Optional[str]) -> Optional[pendulum.DateTime]:
    if not value:
        return None
    return ensure_pendulum_datetime(value)

# modify dates to return datetime objects instead
def _fix_date(ticket):
    ticket["updated_at"] = _parse_date_or_none(ticket["updated_at"])
    ticket["created_at"] = _parse_date_or_none(ticket["created_at"])
    ticket["due_at"] = _parse_date_or_none(ticket["due_at"])
    return ticket

# function from: https://github.com/dlt-hub/verified-sources/tree/master/sources/zendesk
def get_pages(
    url: str,
    endpoint: str,
    auth: Tuple[str, str],
    data_point_name: str,
    params: Optional[Dict[str, Any]] = None,
):
    """
    Makes a request to a paginated endpoint and returns a generator of data items per page.

    Args:
        url: The base URL.
        endpoint: The url to the endpoint, e.g. /api/v2/calls
        auth: Credentials for authentication.
        data_point_name: The key which data items are nested under in the response object (e.g. calls)
        params: Optional dict of query params to include in the request.

    Returns:
        Generator of pages, each page is a list of dict data items.
    """
    # update the page size to enable cursor pagination
    params = params or {}
    params["per_page"] = 1000
    headers = None

    # make request and keep looping until there is no next page
    get_url = f"{url}{endpoint}"
    while get_url:
        response = client.get(
            get_url, headers=headers, auth=auth, params=params
        )
        response.raise_for_status()
        response_json = response.json()
        result = response_json[data_point_name]
        yield result

        get_url = None
        # See https://developer.zendesk.com/api-reference/ticketing/ticket-management/incremental_exports/#json-format
        if not response_json["end_of_stream"]:
            get_url = response_json["next_page"]

if __name__ == "__main__":


    # create a pipeline with an appropriate name
    pipeline = dlt.pipeline(
        pipeline_name="qdrant_zendesk_pipeline",
        destination="qdrant",
        dataset_name="zendesk_data",
    )

    # run the dlt pipeline and save info about the load process
    load_info = pipeline.run(
        # here we use a special function to tell Qdrant which fields to embed
        qdrant_adapter(
            zendesk_support(), # retrieve tickets data
            embed=["subject", "description"],
        )
    )

    print(load_info)


    # running the Qdrant client to connect to your Qdrant database

    @with_config(sections=("destination", "credentials"))
    def get_qdrant_client(location=dlt.secrets.value, api_key=dlt.secrets.value):
        return QdrantClient(
            url=location,
            api_key=api_key,
        )

    # running the Qdrant client to connect to your Qdrant database
    qdrant_client = get_qdrant_client()

    # view Qdrant collections you'll find your dataset here:
    print(qdrant_client.get_collections())

    # query Qdrant with prompt: getting tickets info close to "cancellation"
    response = qdrant_client.query(
        "zendesk_data_content",  # collection/dataset name with the 'content' suffix -> tickets content table
        query_text=["cancel", "cancel subscription"],  # prompt to search
        limit=3  # limit the number of results to the nearest 3 embeddings
    )
