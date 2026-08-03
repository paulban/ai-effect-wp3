# Vendored external protos

`data_synthesizer.proto` is a **copy** of

    use-cases/dutch-node-data-synthesizer/services/data_synthesizer/proto/data_synthesizer.proto

The benchmark service talks to the data synthesizer over gRPC
(`common/benchmark_operations.py` calls `DataSynthesizerServiceStub.GetGridData`),
so it needs that service's message definitions to compile its stubs.

It lives here rather than in `proto/` for two reasons:

- `proto/` must contain **exactly one** `.proto` file — the onboarding export
  generator takes the service's own interface from there, and a second file
  would make which one it picks ambiguous.
- The two services are now separate use cases with separate Docker build
  contexts, so the benchmark build can no longer reach the synthesizer's
  directory.

**This copy can drift.** If `data_synthesizer.proto` changes, re-copy it here
and rebuild the benchmark image.
