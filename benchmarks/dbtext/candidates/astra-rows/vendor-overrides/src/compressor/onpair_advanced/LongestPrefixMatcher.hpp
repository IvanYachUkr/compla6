#pragma once
#include <span>
//---------------------------------------------------------------------------
#include "compressor/onpair_advanced/Infra.hpp"
#include <boost/unordered/unordered_flat_map.hpp>
//---------------------------------------------------------------------------
// Static Global Token Table
// (c) 2025 Nicolas Schmitt
//---------------------------------------------------------------------------
namespace sgtt::compressor::onpair {
//---------------------------------------------------------------------------
/// The Longest Prefix Matcher to build the dictionary
template <MaxSymbolLength, bool = false, uint8_t tokenIdSize = 2>
class LongestPrefixMatcher {
   /// Masks for byte sequences
   static constexpr uint64_t MASKS[] = {
      0x0000000000000000ULL, // 0 bytes
      0x00000000000000FFULL, // 1 byte
      0x000000000000FFFFULL, // 2 bytes
      0x0000000000FFFFFFULL, // 3 bytes
      0x00000000FFFFFFFFULL, // 4 bytes
      0x000000FFFFFFFFFFULL, // 5 bytes
      0x0000FFFFFFFFFFFFULL, // 6 bytes
      0x00FFFFFFFFFFFFFFULL, // 7 bytes
      0xFFFFFFFFFFFFFFFFULL // 8 bytes
   };
   /// Maximum entries per bucket
   static constexpr size_t MAX_BUCKET_SIZE = 128;

   /// The lookup table for short symbol (<= 8 bytes)
   boost::unordered_flat_map<std::pair<uint64_t, uint8_t>, uint16_t, PairHasher> shortLookupTable;
   /// The lookup table for long symbols (> 8 bytes)
   boost::unordered_flat_map<uint64_t, std::vector<std::tuple<uint64_t, uint8_t, uint16_t>>> longLookupTable;
   /// The lengths of the symbols the tokens refer to in bytes - can only be active for a "tokenMatcher"
   std::span<const uint8_t> tokenLengths;
   /// A mapping from dense to sparse IDs; required because IDs in the corpus are dense, but lengths refer to sparse IDs - can only be active for a "tokenMatcher"
   std::span<const uint16_t> tokenMapping;

   /// Mask byte sequence to little-endian u64 with length masking
   /// Note: The caller has to make sure at least 8 bytes are readable.
   static uint64_t mask(const uint8_t* bytes, const size_t len) {
      const uint64_t value = *reinterpret_cast<const uint64_t*>(bytes);
      return value & MASKS[len];
   }
   /// Does what you think it does
   static bool isPrefix(const uint64_t text, const uint64_t prefix, const size_t textLen, const size_t prefixLen) {
      return prefixLen <= textLen && static_cast<size_t>(std::countr_zero(text ^ prefix) >> 3) >= prefixLen;
   }
   /// Computes the byte-length of a token string - inlined for speed
   template <typename SymbolT>
   requires (std::is_same_v<SymbolT, uint64_t> || std::is_same_v<SymbolT, uint128_t>)
   uint8_t computeByteLength(const SymbolT tokens, const size_t tokenLength) const {
      using TokenT = std::conditional_t<tokenIdSize == 1, uint8_t, uint16_t>;
      uint8_t result = 0;
      auto* reader = reinterpret_cast<const uint8_t*>(&tokens);
      auto* mapping = tokenMapping.data();

      const auto end = reader + tokenLength;
      for (; reader + 4 * tokenIdSize <= end; reader += 4 * tokenIdSize) {
         TokenT token0, token1, token2, token3;
         memcpy(&token0, reader + 0 * tokenIdSize, tokenIdSize);
         memcpy(&token1, reader + 1 * tokenIdSize, tokenIdSize);
         memcpy(&token2, reader + 2 * tokenIdSize, tokenIdSize);
         memcpy(&token3, reader + 3 * tokenIdSize, tokenIdSize);
         result += tokenLengths[mapping[token0]];
         result += tokenLengths[mapping[token1]];
         result += tokenLengths[mapping[token2]];
         result += tokenLengths[mapping[token3]];
      }
      for (; reader != end; reader += tokenIdSize) {
         TokenT token;
         memcpy(&token, reader, tokenIdSize);
         result += tokenLengths[mapping[token]];
      }
      return result;
   }

   public:
   /// Constructor
   LongestPrefixMatcher() = default;

   /// Reserve capacity in the lookup tables for up to `tokenCount` tokens
   void reserve(size_t tokenCount) {
      shortLookupTable.reserve(tokenCount);
      longLookupTable.reserve(tokenCount);
   }

   /// Register the token lengths
   void registerTokenLengths(std::span<const uint8_t> tokenLengths, std::span<const uint16_t> tokenMapping);

   /**
     * @brief Inserts a pattern with fixed length constraint.
     * Note: The caller has to make sure at least 8 bytes are readable from the input.
     *
     * @param data Pointer to pattern data
     * @param length Length of pattern in bytes (must be ≤16)
     * @param id Token ID to associate with this pattern
     * @return true if insertion succeeded, false if bucket was full
     */
   bool insert(const uint8_t* data, size_t length, uint16_t id);
   /**
     * @brief Finds the longest matching pattern with fixed length constraint
     *
     * Searches for the longest pattern that matches the beginning of the input data.
     *
     * @param data Pointer to input data to match against
     * @param length Length of input data in bytes
     * @return Optional pair of (token_id, match_length) if match found, nullopt otherwise
     */
   std::optional<std::pair<uint16_t, size_t>> findLongestMatch(const uint8_t* data, size_t length) const;
};
//---------------------------------------------------------------------------
extern template class LongestPrefixMatcher<MaxSymbolLength::EIGHT, false, 1>;
extern template class LongestPrefixMatcher<MaxSymbolLength::SIXTEEN, false, 1>;
extern template class LongestPrefixMatcher<MaxSymbolLength::EIGHT, true, 1>;
extern template class LongestPrefixMatcher<MaxSymbolLength::SIXTEEN, true, 1>;
extern template class LongestPrefixMatcher<MaxSymbolLength::EIGHT, false, 2>;
extern template class LongestPrefixMatcher<MaxSymbolLength::SIXTEEN, false, 2>;
extern template class LongestPrefixMatcher<MaxSymbolLength::EIGHT, true, 2>;
extern template class LongestPrefixMatcher<MaxSymbolLength::SIXTEEN, true, 2>;
//---------------------------------------------------------------------------
}