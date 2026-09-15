// Reproduce the lab's existing prefix/spread Zstd dictionary samplers from raw files.
#include <zdict.h>
#include <algorithm>
#include <cctype>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

namespace fs = std::filesystem;

int main(int argc, char** argv) {
    try {
        if (argc != 4) return 2;
        const std::string mode = argv[3];
        if (mode != "prefix" && mode != "spread") return 2;
        std::vector<fs::path> files;
        for (const auto& entry : fs::directory_iterator(argv[1])) {
            const auto name = entry.path().filename().string();
            if (entry.is_regular_file() && name.size() == 12 && name.substr(8) == ".bin" &&
                std::all_of(name.begin(), name.begin()+8, [](unsigned char c){ return std::isdigit(c); }))
                files.push_back(entry.path());
        }
        std::sort(files.begin(), files.end());
        constexpr size_t chunk = 4096;
        size_t budget = 8*1024*1024;
        std::vector<char> samples;
        std::vector<size_t> sizes;
        samples.reserve(budget);
        for (size_t i = 0; i < files.size() && budget; ++i) {
            const size_t length = fs::file_size(files[i]);
            std::ifstream input(files[i], std::ios::binary);
            if (!input) throw std::runtime_error("cannot read fitting input");
            auto append = [&](size_t position, size_t count) {
                const size_t offset = samples.size();
                samples.resize(offset+count);
                input.seekg(position);
                input.read(samples.data()+offset, count);
                if (!input) throw std::runtime_error("short fitting input");
                sizes.push_back(count);
                budget -= count;
            };
            if (mode == "prefix") {
                const size_t limit = std::min({length, size_t(128*1024), budget});
                for (size_t position = 0; position < limit; position += chunk)
                    append(position, std::min(chunk, limit-position));
            } else {
                const size_t count = std::min(length/chunk, budget/chunk/(files.size()-i));
                for (size_t j = 0; j < count; ++j)
                    append(j*(length-chunk)/std::max(size_t(1), count-1), chunk);
            }
        }
        if (sizes.size() < 8) throw std::runtime_error("insufficient fitting samples");
        std::vector<char> dictionary(mode == "prefix" ? 8192 : 65536);
        const size_t length = ZDICT_trainFromBuffer(dictionary.data(), dictionary.size(),
            samples.data(), sizes.data(), sizes.size());
        if (ZDICT_isError(length) || !length) throw std::runtime_error("dictionary fitting failed");
        std::ofstream output(fs::path(argv[2])/"dictionary.bin", std::ios::binary);
        output.write(dictionary.data(), length);
        if (!output) throw std::runtime_error("dictionary output failed");
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 3;
    }
}
