# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import datetime
import os
import re

import ydb

from airflow import DAG
from airflow.providers.standard.operators.python import PythonOperator
from airflow.providers.ydb.hooks.ydb import YDBHook
from airflow.providers.ydb.operators.ydb import YDBExecuteQueryOperator

from tests_common.test_utils.version_compat import AIRFLOW_V_3_0_PLUS

if AIRFLOW_V_3_0_PLUS:
    from airflow.sdk import task
else:
    # Airflow 2 path
    from airflow.decorators import task  # type: ignore[attr-defined,no-redef]

# [START ydb_operator_howto_guide]

# create_pet_table, populate_pet_table, get_all_pets, and get_birth_date are examples of tasks created by
# instantiating the YDB Operator

ENV_ID = os.environ.get("SYSTEM_TESTS_ENV_ID")
DAG_ID = "ydb_operator_dag"


@task
def populate_pet_table_via_bulk_upsert():
    hook = YDBHook()
    column_types = (
        ydb.BulkUpsertColumns()
        .add_column("pet_id", ydb.OptionalType(ydb.PrimitiveType.Int32))
        .add_column("name", ydb.PrimitiveType.Utf8)
        .add_column("pet_type", ydb.PrimitiveType.Utf8)
        .add_column("birth_date", ydb.PrimitiveType.Utf8)
        .add_column("owner", ydb.PrimitiveType.Utf8)
    )

    rows = [
        {"pet_id": 3, "name": "Lester", "pet_type": "Hamster", "birth_date": "2020-06-23", "owner": "Lily"},
        {"pet_id": 4, "name": "Quincy", "pet_type": "Parrot", "birth_date": "2013-08-11", "owner": "Anne"},
    ]
    hook.bulk_upsert("pet", rows=rows, column_types=column_types)


def sanitize_date(value: str) -> str:
    """Ensure the value is a valid date format"""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"Invalid date format: {value}")
    return value


def transform_dates_func(**kwargs):
    begin_date = sanitize_date(kwargs.get("begin_date"))
    end_date = sanitize_date(kwargs.get("end_date"))
    return {"begin_date": begin_date, "end_date": end_date}


with DAG(
    dag_id=DAG_ID,
    start_date=datetime.datetime(2020, 2, 2),
    schedule="@once",
    catchup=False,
) as dag:
    # [START ydb_operator_howto_guide_create_pet_table]
    create_pet_table = YDBExecuteQueryOperator(
        task_id="create_pet_table",
        sql="""
            CREATE TABLE pet (
            pet_id INT,
            name TEXT NOT NULL,
            pet_type TEXT NOT NULL,
            birth_date TEXT NOT NULL,
            owner TEXT NOT NULL,
            PRIMARY KEY (pet_id)
            );
          """,
        is_ddl=True,  # must be specified for DDL queries
    )

    # [END ydb_operator_howto_guide_create_pet_table]
    # [START ydb_operator_howto_guide_populate_pet_table]
    populate_pet_table = YDBExecuteQueryOperator(
        task_id="populate_pet_table",
        sql="""
              UPSERT INTO pet (pet_id, name, pet_type, birth_date, owner)
              VALUES (1, 'Max', 'Dog', '2018-07-05', 'Jane');

              UPSERT INTO pet (pet_id, name, pet_type, birth_date, owner)
              VALUES (2, 'Susie', 'Cat', '2019-05-01', 'Phil');
            """,
    )
    # [END ydb_operator_howto_guide_populate_pet_table]
    # [START ydb_operator_howto_guide_get_all_pets]
    get_all_pets = YDBExecuteQueryOperator(task_id="get_all_pets", sql="SELECT * FROM pet;")
    # [END ydb_operator_howto_guide_get_all_pets]
    transform_dates = PythonOperator(
        task_id="transform_dates",
        python_callable=transform_dates_func,
        op_kwargs={"begin_date": "{{params.begin_date}}", "end_date": "{{params.end_date}}"},
        params={"begin_date": "2020-01-01", "end_date": "2020-12-31"},
    )
    # [START ydb_operator_howto_guide_get_birth_date]
    get_birth_date = YDBExecuteQueryOperator(
        task_id="get_birth_date",
        sql="""
        SELECT * FROM pet WHERE birth_date BETWEEN '{{ ti.xcom_pull(task_ids="transform_dates")["begin_date"] }}' AND '{{ ti.xcom_pull(task_ids="transform_dates")["end_date"] }}'
        """,
    )
    # [END ydb_operator_howto_guide_get_birth_date]

    (
        create_pet_table
        >> populate_pet_table
        >> populate_pet_table_via_bulk_upsert()
        >> get_all_pets
        >> transform_dates
        >> get_birth_date
    )
    # [END ydb_operator_howto_guide]

    from tests_common.test_utils.watcher import watcher

    # This test needs watcher in order to properly mark success/failure
    # when "tearDown" task with trigger rule is part of the DAG
    list(dag.tasks) >> watcher()

from tests_common.test_utils.system_tests import get_test_run  # noqa: E402

# Needed to run the example DAG with pytest (see: tests/system/README.md#run_via_pytest)
test_run = get_test_run(dag)
