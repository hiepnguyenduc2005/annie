//
//  Keychain.swift
//  AnnieApp
//
//  The only place API keys are kept on this device. ElevenLabs and Deepgram
//  keys are real, billable credentials, so they go in the Keychain — never
//  UserDefaults, never a file, never a log line.
//
//  Items are generic passwords under one service name, readable only while
//  the phone is unlocked and never included in backups or synced to another
//  device (`WhenUnlockedThisDeviceOnly`).
//

import Foundation
import Security

enum Keychain {
    static let service = "app.annie.companion.keys"

    /// The accounts this app stores. Raw values are the Keychain account names.
    enum Account: String {
        case elevenLabs = "elevenlabs_api_key"
        case deepgram = "deepgram_api_key"
    }

    /// Save (or replace) a secret. Returns false if the Keychain refused.
    @discardableResult
    static func set(_ value: String, for account: Account) -> Bool {
        guard let data = value.data(using: .utf8) else { return false }

        let status = SecItemUpdate(query(account) as CFDictionary, [kSecValueData as String: data] as CFDictionary)
        if status == errSecSuccess { return true }
        guard status == errSecItemNotFound else { return false }

        var item = query(account)
        item[kSecValueData as String] = data
        item[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
        return SecItemAdd(item as CFDictionary, nil) == errSecSuccess
    }

    static func get(_ account: Account) -> String? {
        var lookup = query(account)
        lookup[kSecReturnData as String] = true
        lookup[kSecMatchLimit as String] = kSecMatchLimitOne

        var result: CFTypeRef?
        guard SecItemCopyMatching(lookup as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    /// True when a secret is stored, without reading it into memory.
    static func has(_ account: Account) -> Bool {
        var lookup = query(account)
        lookup[kSecMatchLimit as String] = kSecMatchLimitOne
        return SecItemCopyMatching(lookup as CFDictionary, nil) == errSecSuccess
    }

    /// Returns true when the item is gone afterwards (including "was never there").
    @discardableResult
    static func remove(_ account: Account) -> Bool {
        let status = SecItemDelete(query(account) as CFDictionary)
        return status == errSecSuccess || status == errSecItemNotFound
    }

    private static func query(_ account: Account) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account.rawValue,
        ]
    }
}
