"""Public API（M2 onboarding 後端）：SIWE 登入、產待簽 payload（前端簽完直送 HL）、
verify（鏈上查詢）、admin 唯讀。非託管不變量：主鑰與 EIP-712 授權簽名永不進本套件
任何路徑；agent 私鑰只在 key-service。"""
