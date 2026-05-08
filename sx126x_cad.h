#pragma once

#include "esphome/components/sx126x/sx126x.h"
#include "esphome/core/log.h"

namespace esphome {
namespace sx126x_cad {

static const uint8_t CAD_DET_PEAK[6] = {22, 22, 24, 25, 26, 30}; // SF7..SF12, AN1200.48

class SX126xCad : public esphome::sx126x::SX126x {
 public:

  bool is_channel_clear() {
    
  }
};

} // namespace sx126x_cad
} // namespace esphome


inline bool clear_air_check(esphome::sx126x::SX126x *snx) {
  auto *radio = static_cast<esphome::sx126x_cad::SX126xCad *>(snx);
  for (int i = 0; i < 5; ++i) {
    if (radio->is_channel_clear()) {
      ESP_LOGD("cc", "channel clear (attempt %d/5)", i + 1);
      return true;
    }
    ESP_LOGD("cc", "channel busy (attempt %d/5)", i + 1);
    vTaskDelay(pdMS_TO_TICKS(50));
  }
  ESP_LOGW("cc", "channel busy after 5 attempts — sending anyway");
  return true;
}