// Counts a seed-dependent number of enabled cycles and checks the result.
`include "rtlfarm_tb.svh"

`ifndef RTLFARM_TIMEOUT_SIM
`define RTLFARM_TIMEOUT_SIM 10ms
`endif

module tb_counter;
  `RTLFARM_INIT
  `RTLFARM_WATCHDOG(`RTLFARM_TIMEOUT_SIM)

  localparam int WIDTH = 8;

  logic             clk = 1'b0;
  logic             rst_n;
  logic             en;
  logic [WIDTH-1:0] count;

  always #5 clk = ~clk;

  counter #(.WIDTH(WIDTH)) dut (
      .clk  (clk),
      .rst_n(rst_n),
      .en   (en),
      .count(count)
  );

  integer cycles;
  integer expected;

  initial begin
    `RTLFARM_SEED_PROCESS(1)
    rst_n = 1'b0;
    en = 1'b0;
    repeat (2) @(posedge clk);
    @(negedge clk) rst_n = 1'b1;
    cycles = 5 + ($urandom % 50);
    expected = 0;
    repeat (cycles) begin
      @(negedge clk);
      en = 1'b1;
      expected = expected + 1;
    end
    @(negedge clk) en = 1'b0;
    @(negedge clk);
    if (count !== expected[WIDTH-1:0]) `RTLFARM_FAIL("count does not match the enabled cycles")
    `RTLFARM_PASS
  end
endmodule
