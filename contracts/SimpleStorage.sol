// SPDX-License-Identifier: MIT

pragma solidity >=0.6.0 <0.9.0;

contract SimpleStorage {

    uint256 favoriteNumber;

    // create a "person" type
    struct People {
        uint256 favoriteNumber;
        string name;
    }

    // create a array for a data type called Person and make a mapping over it
    People[] public people;
    mapping(string => uint256) public nameToFavoriteNumber;

    //store a favourite numeber
    function store(uint256 _favoriteNumber) public {
        favoriteNumber = _favoriteNumber;
    }
    
    //return the vavourite number
    function retrieve() public view returns (uint256){
        return favoriteNumber;
    }

    //Make a instance of a Person data type
    function addPerson(string memory _name, uint256 _favoriteNumber) public {
        people.push(People(_favoriteNumber, _name));
        nameToFavoriteNumber[_name] = _favoriteNumber;
    }
}