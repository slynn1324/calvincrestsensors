#pragma once
#include <string>
#include <vector>
#include <cstdlib>  // strtof, strtol, strtoul

struct LoraMsg {
  bool valid = false;
  std::string command;
  std::string sender;
  unsigned long sender_time = 0;
  float rssi = 0.0f;
  float snr = 0.0f;
  std::vector<std::string> params;

  std::string param(size_t i) const {
    return i < params.size() ? params[i] : "";
  }

  float param_float(size_t i, float default_val = 0.0f) const {
    if (i >= params.size()) return default_val;
    const char* s = params[i].c_str();
    char* end;
    float val = strtof(s, &end);
    return (end != s) ? val : default_val;  // end==s means no digits were consumed
  }

  int param_int(size_t i, int default_val = 0) const {
    if (i >= params.size()) return default_val;
    const char* s = params[i].c_str();
    char* end;
    long val = strtol(s, &end, 10);
    return (end != s) ? (int)val : default_val;
  }

};

inline LoraMsg parse_lora_msg(const std::string& raw, float rssi, float snr) {
  LoraMsg result;

  std::vector<std::string> parts;
  size_t start = 0, end = 0;
  while ((end = raw.find('|', start)) != std::string::npos) {
    parts.push_back(raw.substr(start, end - start));
    start = end + 1;
  }
  parts.push_back(raw.substr(start));

  if (parts.size() < 3) return result;

  result.command = parts[0];
  result.sender = parts[1];

  // parse sender_time safely without exceptions
  const char* s = parts[2].c_str();
  char* end_ptr;
  unsigned long t = strtoul(s, &end_ptr, 10);
  if (end_ptr == s) return result;  // no digits consumed → malformed
  result.sender_time = t;

  for (size_t i = 3; i < parts.size(); i++) {
    result.params.push_back(parts[i]);
  }

  result.rssi = rssi;
  result.snr = snr;
  result.valid = true;
  return result;
}
