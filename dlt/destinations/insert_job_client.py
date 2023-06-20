import os
from typing import Any, Iterator, List

import pickle
import sqlglot.expressions as exp

from dlt.common.destination.reference import LoadJob, FollowupJob, TLoadJobState
from dlt.common.schema.typing import TTableSchema, TWriteDisposition
from dlt.common.storages import FileStorage

from dlt.destinations.sql_client import SqlClientBase
from dlt.destinations.job_impl import EmptyLoadJob
from dlt.destinations.job_client_impl import SqlJobClientBase


class InsertValuesLoadJob(LoadJob, FollowupJob):
    def __init__(self, table_name: str, write_disposition: TWriteDisposition, file_path: str, sql_client: SqlClientBase[Any]) -> None:
        super().__init__(FileStorage.get_file_name_from_file_path(file_path))
        self._sql_client = sql_client
        # insert file content immediately
        with self._sql_client.with_staging_dataset(write_disposition=="merge"):
            with self._sql_client.begin_transaction():
                for fragments in self._insert(sql_client.make_qualified_table_name(table_name), write_disposition, file_path):
                    self._sql_client.execute_fragments(fragments)

    def state(self) -> TLoadJobState:
        # this job is always done
        return "completed"

    def exception(self) -> str:
        # this part of code should be never reached
        raise NotImplementedError()

    def _insert(self, qualified_table_name: str, write_disposition: TWriteDisposition, file_path: str) -> Iterator[List[str]]:
        # WARNING: maximum redshift statement is 16MB https://docs.aws.amazon.com/redshift/latest/dg/c_redshift-sql.html
        # the procedure below will split the inserts into max_query_length // 2 packs
        with FileStorage.open_zipsafe_ro(file_path, "rb") as f:
            ast: exp.Insert = pickle.load(f)
            ast.find(exp.Placeholder).replace(exp.to_table(qualified_table_name))
            ast.args["expression"] # <- this is the SELECT ... FROM VALUES, TODO: we should split it into chunks if needed
            stmt = []
            if write_disposition == "replace":
                exp.delete(qualified_table_name)
                stmt.append(exp.delete(qualified_table_name).sql(dialect="duckdb"))
            stmt.append(ast.sql(dialect="duckdb"))  # TODO: we should really have destination name to derive the right dialect
            yield stmt


class InsertValuesJobClient(SqlJobClientBase):

    def restore_file_load(self, file_path: str) -> LoadJob:
        """Returns a completed SqlLoadJob or InsertValuesJob

        Returns completed jobs as SqlLoadJob and InsertValuesJob executed atomically in start_file_load so any jobs that should be recreated are already completed.
        Obviously the case of asking for jobs that were never created will not be handled. With correctly implemented loader that cannot happen.

        Args:
            file_path (str): a path to a job file

        Returns:
            LoadJob: Always a restored job completed
        """
        job = super().restore_file_load(file_path)
        if not job:
            job = EmptyLoadJob.from_file_path(file_path, "completed")
        return job

    def start_file_load(self, table: TTableSchema, file_path: str, load_id: str) -> LoadJob:
        job = super().start_file_load(table, file_path, load_id)
        if not job:
            # this is using sql_client internally and will raise a right exception
            job = InsertValuesLoadJob(table["name"], table["write_disposition"], file_path, self.sql_client)
        return job

    # TODO: implement indexes and primary keys for postgres
    def _get_in_table_constraints_sql(self, t: TTableSchema) -> str:
        # get primary key
        pass

    def _get_out_table_constrains_sql(self, t: TTableSchema) -> str:
        # set non unique indexes
        pass
