-- PGroongaのサーバー側ファイルを導入した後、対象DBごとに実行する。
-- 既存のアプリテーブル・データ・公開状態は変更しない。
-- 新規導入はsearch_pathに依存せずpublicへ配置する。既存拡張は移動・更新しない。
CREATE EXTENSION IF NOT EXISTS pgroonga WITH SCHEMA public;
