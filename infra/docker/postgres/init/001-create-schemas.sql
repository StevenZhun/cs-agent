\set ON_ERROR_STOP on

CREATE SCHEMA IF NOT EXISTS langgraph;
CREATE SCHEMA IF NOT EXISTS app;
CREATE SCHEMA IF NOT EXISTS business;
CREATE SCHEMA IF NOT EXISTS content;
CREATE SCHEMA IF NOT EXISTS quality;

COMMENT ON SCHEMA langgraph IS 'LangGraph checkpointer framework-owned tables';
COMMENT ON SCHEMA app IS 'Identity, conversation, messages, runs, feedback and wrap-up facts';
COMMENT ON SCHEMA business IS 'Customer profiles and service records provided by the customer data adapter';
COMMENT ON SCHEMA content IS 'Authoritative intent, FAQ, SOP and RAG content versions';
COMMENT ON SCHEMA quality IS 'Synchronous guard and asynchronous quality inspection results';
