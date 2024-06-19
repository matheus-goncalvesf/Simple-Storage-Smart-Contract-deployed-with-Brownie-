from brownie import SimpleStorage, accounts

#rodar testes do contrato

def test_deploy():
    # 1 - Arrange
    account = accounts[0]

    # 2 - act
    simple_storage = SimpleStorage.deploy({"from": account})
    starting_value = simple_storage.retrieve()
    expected = 0

    # 3 - assert
    assert starting_value == expected



