import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

load_dotenv(Path(__file__).with_name('.env'))


class DatabaseError(RuntimeError):
    """Safe database error that never exposes connection credentials."""


def get_connection():
    url = os.getenv('DATABASE_URL', '').strip()
    if not url:
        raise DatabaseError('DATABASE_URL 환경변수가 설정되지 않았습니다.')
    try:
        return psycopg.connect(
            url, connect_timeout=10, row_factory=dict_row,
<<<<<<< HEAD
=======
            # options='-c statement_timeout=10000',
>>>>>>> b05369d498b67155d44781fbe051153adcd387bd
        )
    except psycopg.Error as error:
        raise DatabaseError('DB 연결 실패: DATABASE_URL, 인증 정보 및 네트워크를 확인하세요.') from error


def fetch_one(query: str):
    try:
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '10s'")
                cursor.execute(query)
                return cursor.fetchone()
    except psycopg.Error as error:
        raise DatabaseError('DB 쿼리 실행 실패: 연결 상태와 기존 테이블을 확인하세요.') from error


def fetch_all(query: str, params: tuple = ()) -> list[dict]:
    try:
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '10s'")
                cursor.execute(query, params)
                return cursor.fetchall()
    except psycopg.Error as error:
        raise DatabaseError('DB 검색 실패: 연결 상태와 기존 테이블을 확인하세요.') from error


def execute(query: str, params: tuple = ()) -> None:
    try:
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '10s'")
                cursor.execute(query, params)
    except psycopg.Error as error:
        raise DatabaseError('DB 기록 실패: 연결 상태와 기존 테이블을 확인하세요.') from error
