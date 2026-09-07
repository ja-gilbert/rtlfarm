// RTLFARM_INIT is a module-item macro: instantiate it once at module scope in the
// testbench top, never inside a procedural block.
`define RTLFARM_INIT \
  integer rtlfarm_seed = 1; bit rtlfarm_seed_ready = 1'b0; \
  initial begin \
    if (!$value$plusargs("seed=%d", rtlfarm_seed)) rtlfarm_seed = 1; \
    void'($urandom(rtlfarm_seed)); \
    if ($test$plusargs("dump")) begin $dumpfile("wave.fst"); $dumpvars(0); end \
    rtlfarm_seed_ready = 1'b1; \
  end
// Every process that draws random numbers starts with this, with a distinct k:
// it waits for the seed to be read, then seeds its own generator.
`define RTLFARM_SEED_PROCESS(k) \
  begin integer rtlfarm_process_seed; wait (rtlfarm_seed_ready); \
    rtlfarm_process_seed = rtlfarm_seed + (k); void'($urandom(rtlfarm_process_seed)); end
`define RTLFARM_WATCHDOG(limit) initial begin #(limit); $display("RTLFARM: WATCHDOG t=%0t", $time); $finish; end
`define RTLFARM_PASS  begin $display("RTLFARM: PASS t=%0t", $time); $finish; end
`define RTLFARM_FAIL(msg) begin $display("RTLFARM: FAIL t=%0t %s", $time, msg); $finish; end
