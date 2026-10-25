-- 新規のローカル開発DB用。既存ボリュームには手動でCREATE EXTENSIONを実行します。
CREATE EXTENSION IF NOT EXISTS vector;

-- 実接続テストはアプリの検証データと別のDBで実行します。
CREATE DATABASE file_scan_test OWNER file_scan;
\connect file_scan_test
CREATE EXTENSION IF NOT EXISTS vector;
