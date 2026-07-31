from __future__ import annotations
from pathlib import Path, PurePosixPath
from omnigraph.extract import extract_sql


def _quote_ident(name: str) -> str:
    """Double-quote a PostgreSQL identifier, escaping embedded double-quotes."""
    return '"' + name.replace('"', '""') + '"'


def introspect_postgres(dsn: str | None = None) -> dict:
    """Connect to PostgreSQL, reconstruct DDL, and extract via extract_sql()."""
    try:
        import psycopg
    except ModuleNotFoundError:
        raise ImportError(
            "psycopg is required for --postgres. "
            "Install with: pip install 'omnigraph[postgres]'"
        )

    try:
        conn = psycopg.connect(dsn or "")  # empty string = PG* env vars
    except psycopg.OperationalError as exc:
        # Sanitizar: remova o DSN/credenciais que o psycopg pode incorporar no
        # Mensagem OperationalError (por exemplo, "conexão ao servidor… falhou: …\nDETAIL: …")
        msg = str(exc).split("\n")[0]
        raise ConnectionError(f"could not connect to PostgreSQL: {msg}") from None

    try:
        conn.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE READ ONLY DEFERRABLE")

        # 1. Query tables
        with conn.cursor() as cur:
            cur.execute("""
                SELECT table_schema, table_name, table_type
                FROM information_schema.tables
                WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
                ORDER BY table_schema, table_name;
            """)
            tables = cur.fetchall()

            # 2. Query views
            cur.execute("""
                SELECT table_schema, table_name, view_definition
                FROM information_schema.views
                WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
                ORDER BY table_schema, table_name;
            """)
            views = cur.fetchall()

            # 3. Query routines (functions/procedures), including language
            cur.execute("""
                SELECT routine_schema, routine_name, routine_type,
                       routine_definition, external_language
                FROM information_schema.routines
                WHERE routine_schema NOT IN ('pg_catalog', 'information_schema')
                ORDER BY routine_schema, routine_name;
            """)
            routines = cur.fetchall()

            # 4. Consultar chaves estrangeiras — agrupadas por restrição para lidar com composições.
            # Leia pg_catalog.pg_constraint, NÃO information_schema.referential_
            # restrições: essa visualização mostra apenas restrições onde o atual
            # usuário tem acesso WRITE à tabela de referência (proprietário ou um
            # privilégio diferente de SELECT), portanto, uma função de introspecção somente leitura
            # vê zero linhas FK enquanto todas as tabelas/visualizações/rotinas aparecem - o
            # o grafo perde silenciosamente todas as arestas de 'referências' (# 1746).
            # pg_constraint não é filtrado por privilégios. Ele também define restrições
            # por oid em vez de por nome (nomes de restrições são exclusivos apenas por
            # tabela, então as antigas junções key_column_usage baseadas em nome poderiam
            # fazer correspondência cruzada de restrições com o mesmo nome em tabelas irmãs).
            cur.execute("""
                SELECT
                    con.conname AS constraint_name,
                    ns.nspname AS table_schema,
                    rel.relname AS table_name,
                    (SELECT ARRAY_AGG(att.attname ORDER BY k.ord)
                       FROM UNNEST(con.conkey) WITH ORDINALITY AS k(attnum, ord)
                       JOIN pg_catalog.pg_attribute att
                         ON att.attrelid = con.conrelid AND att.attnum = k.attnum
                    ) AS columns,
                    fns.nspname AS foreign_table_schema,
                    frel.relname AS foreign_table_name,
                    (SELECT ARRAY_AGG(att.attname ORDER BY k.ord)
                       FROM UNNEST(con.confkey) WITH ORDINALITY AS k(attnum, ord)
                       JOIN pg_catalog.pg_attribute att
                         ON att.attrelid = con.confrelid AND att.attnum = k.attnum
                    ) AS foreign_columns
                FROM pg_catalog.pg_constraint con
                JOIN pg_catalog.pg_class rel ON rel.oid = con.conrelid
                JOIN pg_catalog.pg_namespace ns ON ns.oid = rel.relnamespace
                JOIN pg_catalog.pg_class frel ON frel.oid = con.confrelid
                JOIN pg_catalog.pg_namespace fns ON fns.oid = frel.relnamespace
                WHERE con.contype = 'f'
                  AND ns.nspname NOT IN ('pg_catalog', 'information_schema')
                ORDER BY ns.nspname, rel.relname, con.conname;
            """)
            fks = cur.fetchall()
    finally:
        conn.close()

    ddl = []

    # Tabelas — identificadores de cotação para lidar com palavras reservadas, hífens, letras maiúsculas e minúsculas
    for schema, name, ttype in tables:
        if ttype == "BASE TABLE":
            ddl.append(f"CREATE TABLE {_quote_ident(schema)}.{_quote_ident(name)} (id INT);")

    # Visualizações — corpo real se disponível, stub se NULL (permissão negada)
    for schema, name, body in views:
        if body:
            ddl.append(f"CREATE VIEW {_quote_ident(schema)}.{_quote_ident(name)} AS {body};")
        else:
            ddl.append(f"CREATE VIEW {_quote_ident(schema)}.{_quote_ident(name)} AS SELECT 1;")

    # FK edges — one ALTER TABLE per constraint (handles composite FKs correctly).
    # Emitted BEFORE the function DDL: routine bodies the grammar can't parse
    # (notably C-language extension functions, whose "body" is just the C symbol
    # name) put tree-sitter into error recovery that consumes the statements
    # after them — with FKs last, every 'references' edge is silently lost on
    # any DB with a common extension installed. FK statements only
    # reference tables, which are emitted first, so this order is always safe.
    for constraint_name, t_schema, t_name, cols, r_schema, r_name, r_cols in fks:
        col_list = ", ".join(_quote_ident(c) for c in cols)
        ref_col_list = ", ".join(_quote_ident(c) for c in r_cols)
        ddl.append(
            f"ALTER TABLE {_quote_ident(t_schema)}.{_quote_ident(t_name)} "
            f"ADD CONSTRAINT {_quote_ident(constraint_name)} "
            f"FOREIGN KEY ({col_list}) REFERENCES {_quote_ident(r_schema)}.{_quote_ident(r_name)}({ref_col_list});"
        )

    # Funções e Procedimentos — corpo real se disponível, stub se NULL
    # Use $gfx$ como tag de cotação de dólar para evitar colisão com $$ dentro dos corpos.
    # Use external_language do catálogo; volte para plpgsql se NULL/blank.
    for schema, name, rtype, body, ext_lang in routines:
        lang = (ext_lang or "plpgsql").lower()
        fn_sig = f"{_quote_ident(schema)}.{_quote_ident(name)}()"
        stub_body = "BEGIN SELECT 1; END;"
        if rtype in ("FUNCTION", "PROCEDURE"):
            actual_body = body if body else stub_body
            # Represente PROCEDUREs como FUNCTION para que tree-sitter-sql possa analisá-los
            ddl.append(
                f"CREATE FUNCTION {fn_sig} RETURNS void"
                f" AS $gfx$ {actual_body} $gfx$ LANGUAGE {lang};"
            )

    ddl_string = "\n".join(ddl)

    # Determinar host/nome do banco de dados para limpeza de DSN de caminho virtual
    info = psycopg.conninfo.conninfo_to_dict(dsn or "")
    host = info.get("host", "localhost")
    dbname = info.get("dbname", "db")
    virtual_path = PurePosixPath(f"postgresql://{host}/{dbname}")

    # Passe o caminho virtual e o conteúdo DDL na memória para extract_sql
    result = extract_sql(virtual_path, content=ddl_string)
    return result