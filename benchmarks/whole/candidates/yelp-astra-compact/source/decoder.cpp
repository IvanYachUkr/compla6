#include "common_io.hpp"
#include "core.hpp"
int main(int argc, char **argv) {
  return transport::run_cli(argc, argv, false, sw::decode);
}
