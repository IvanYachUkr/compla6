#include "common_io.hpp"
#include "encode.hpp"
int main(int argc, char **argv) {
  return transport::run_cli(argc, argv, true, sw::encode);
}
